"""MCP server: the toll gate exposed to AI agents over Model Context Protocol.

Transport: streamable HTTP, mounted into the FastAPI app at /mcp (see main.py).

Authentication
--------------
MCP has no session cookie story for us; every HTTP request to /mcp carries
the agent's toll-gate API key, either as ``Authorization: Bearer tg_...`` or
``X-API-Key: tg_...``. Tools resolve the key to (agent, policy) with the SAME
logic as gateway/auth.py: SHA-256 hash → indexed lookup → revoked/inactive
checks → rate limits. Every rejection is audited before the error is raised
("commit before you speak" applies to evidence too).

Policy enforcement
------------------
Read-only tools (get_transaction, list_disputes, get_dispute) enforce agent
identity + rate limits. Money-moving tools (issue_refund, create_order)
additionally run the full policy engine pipeline — allowlist, caps, rolling
window, approval threshold — exactly like POST /v1/agent-actions. An
``allowed`` refund is then executed against PayPal with a DETERMINISTIC
PayPal-Request-Id derived from the action id, so a retry after a timeout
can never double-refund.

Results
-------
Every tool returns a JSON-compatible envelope:
    {"ok": True, "summary": "<human-readable one-liner>", ...structured data}
Decisions that came from the policy engine (denied / pending_approval /
deduplicated) are RESULTS, not errors — ok stays True, mirroring the HTTP
API's "200 for all four outcomes" convention. Protocol-level failures (auth,
validation, PayPal errors) raise ToolError with a "REASON_CODE: message"
string, which MCP surfaces as an isError tool result.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway import audit
from gateway import approvals as approvals_mod
from gateway.api_keys import hash_api_key
from gateway.db import session_scope
from gateway.models import Agent, AgentKey, Approval, Policy
from gateway.paypal_client import (
    PayPalAPIError,
    PayPalClient,
    PayPalError,
    get_approval_url,
)
from gateway.policy_engine import ActionRequest as EngineRequest
from gateway.policy_engine import _emit, authorize
from gateway.redis_client import RateLimitExceeded, check_rate_limits
from gateway.schemas import StrictDecimal

logger = logging.getLogger(__name__)

mcp = FastMCP(name="tollgate")

# Fixed namespace so PayPal-Request-Ids are deterministic per action:
# uuid5(NS, "tollgate-refund-action-42") is the same forever, everywhere.
_PAYPAL_REQUEST_NS = uuid.UUID("7d444840-9dc0-11d1-b245-5ffdce74fad2")


# ---------------------------------------------------------------------------
# Strict input types (Pydantic constraints → the MCP tool input schema)
# ---------------------------------------------------------------------------

TransactionId = Annotated[
    str,
    Field(
        pattern=r"^[A-Z0-9]{17}$",
        description="PayPal transaction/capture/order/refund ID: exactly 17 "
                    "uppercase alphanumeric characters, e.g. 05Y454980K6595141.",
    ),
]

DisputeId = Annotated[
    str,
    Field(
        pattern=r"^PP-D-\d{1,12}$",
        description="PayPal dispute ID, e.g. PP-D-56244.",
    ),
]

IdempotencyKey = Annotated[
    str,
    Field(
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Caller-generated unique key (8-64 chars, [A-Za-z0-9_-]). "
                    "Replays return the original decision — money never moves twice.",
    ),
]

Amount = Annotated[
    StrictDecimal,
    Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
        description='Positive decimal STRING, e.g. "10.99". Must be > 0. '
                    "JSON numbers are rejected (floats corrupt cents).",
    ),
]

Currency = Literal["USD", "EUR", "GBP", "CAD", "AUD", "INR"]

StartTime = Annotated[
    str,
    Field(
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$",
        description="RFC 3339 UTC timestamp, e.g. 2026-10-01T00:00:00Z.",
    ),
]


# ---------------------------------------------------------------------------
# PayPal client injection (main.py lifespan; tests inject a MockTransport one)
# ---------------------------------------------------------------------------

_paypal: PayPalClient | None = None


def init_paypal_client(client: PayPalClient) -> None:
    global _paypal
    _paypal = client


def _get_paypal() -> PayPalClient:
    if _paypal is None:
        raise ToolError("SERVER_MISCONFIGURED: no PayPal client was injected "
                        "at startup — the MCP tools cannot call PayPal.")
    return _paypal


def _describe_paypal_error(exc: PayPalError) -> str:
    """One-line, debug_id-carrying description for a ToolError message."""
    if isinstance(exc, PayPalAPIError):
        return (f"[{exc.status_code}] {exc.name}: {exc.message} "
                f"(debug_id={exc.debug_id})")
    return str(exc)


# ---------------------------------------------------------------------------
# Authentication (same rules + reason codes as gateway/auth.py, no FastAPI)
# ---------------------------------------------------------------------------

def _audit_reject(session: Session, reason: str, detail: dict,
                  agent_id: int | None = None) -> ToolError:
    """Commit the failure audit immediately, then build the ToolError.

    Mirrors auth._audit_and_reject: raising unwinds the tool's transaction,
    so the evidence row must be committed BEFORE the raise.
    """
    decision = "rate_limited" if reason.startswith("RATE_LIMIT") else "auth_failed"
    audit.record(session, decision=decision, reason_code=reason,
                 agent_id=agent_id, detail={"via": "mcp", **detail})
    session.commit()
    return ToolError(f"{reason}: {_REJECT_HINTS.get(reason, 'request rejected')}")


_REJECT_HINTS = {
    "MISSING_API_KEY": "send Authorization: Bearer tg_... or X-API-Key on every request",
    "INVALID_API_KEY": "the presented key does not exist",
    "API_KEY_REVOKED": "this key was revoked; ask the operator to issue a new one",
    "AGENT_INACTIVE": "the agent owning this key is suspended",
    "POLICY_MISSING": "provisioning bug: agent has no policy row (failing closed)",
    "RATE_LIMIT_EXCEEDED": "too many requests this minute; back off and retry",
}


def _extract_key(headers: dict[str, str]) -> str | None:
    api_key = headers.get("x-api-key", "")
    if api_key.strip():
        return api_key.strip()
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _authenticate(session: Session) -> tuple[Agent, Policy]:
    """Resolve the calling agent from request headers, then rate-limit.

    Raises ToolError (audited) on any rejection. Returns (agent, policy)
    attached to this session.
    """
    # get_http_headers() redacts credential headers by default (it is built
    # for proxies that must not forward them); opt in explicitly.
    headers = get_http_headers(include={"authorization"})
    key = _extract_key({k.lower(): v for k, v in headers.items()})
    if not key:
        raise _audit_reject(session, "MISSING_API_KEY",
                            {"hint": "no Authorization/X-API-Key header"})

    key_row = session.execute(
        select(AgentKey).where(AgentKey.key_hash == hash_api_key(key))
    ).scalar_one_or_none()

    if key_row is None:
        # Prefix only in the audit row — never the key or its hash.
        raise _audit_reject(session, "INVALID_API_KEY", {"key_prefix": key[:11]})
    if key_row.revoked_at is not None:
        raise _audit_reject(session, "API_KEY_REVOKED",
                            {"key_prefix": key[:11], "key_id": key_row.id},
                            agent_id=key_row.agent_id)

    agent = session.get(Agent, key_row.agent_id)
    if not agent.is_active:
        raise _audit_reject(session, "AGENT_INACTIVE",
                            {"agent_id": agent.id}, agent_id=agent.id)

    policy = session.execute(
        select(Policy).where(Policy.agent_id == agent.id)
    ).scalar_one_or_none()
    if policy is None:
        logger.error("Agent %s has no policy row — failing closed", agent.id)
        raise _audit_reject(session, "POLICY_MISSING",
                            {"agent_id": agent.id}, agent_id=agent.id)

    try:
        check_rate_limits(agent.id)
    except RateLimitExceeded as exc:
        raise _audit_reject(session, "RATE_LIMIT_EXCEEDED",
                            {"scope": exc.scope, "limit_per_min": exc.limit,
                             "retry_after": exc.retry_after_seconds},
                            agent_id=agent.id) from exc

    return agent, policy


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def _decision_payload(decision) -> dict:
    """JSON-safe view of a policy-engine Decision (Decimals as strings)."""
    return {
        "outcome": decision.outcome,
        "reason_code": decision.reason_code,
        "action_id": decision.action_id,
        "approval_id": decision.approval_id,
        "duplicate_of_action_id": decision.duplicate_of_action_id,
        "original_outcome": decision.original_outcome,
        "rolling_24h_used": str(decision.rolling_24h_used),
        "rolling_24h_cap": str(decision.rolling_24h_cap),
    }


def _money(d: dict | None) -> dict:
    """PayPal {"amount": {"value": "7.99", "currency_code": "USD"}} → flat dict."""
    d = d or {}
    return {"value": d.get("value"), "currency": d.get("currency_code")}


def _not_allowed_envelope(decision, amount: Decimal, currency: str,
                          verb: str) -> dict:
    """denied / pending_approval / deduplicated are results, not errors."""
    summaries = {
        "denied": f"{verb} of {amount} {currency} DENIED ({decision.reason_code}).",
        "pending_approval": (f"{verb} of {amount} {currency} awaits human approval "
                             f"(approval #{decision.approval_id}); it reserves cap "
                             f"headroom until decided or expired."),
        "deduplicated": (f"Duplicate request — the original decision was "
                         f"'{decision.original_outcome}' "
                         f"(action #{decision.duplicate_of_action_id})."),
    }
    return {"ok": True, "summary": summaries[decision.outcome],
            "decision": _decision_payload(decision)}


# ---------------------------------------------------------------------------
# Tools: read-only (identity + rate limit enforced)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_transaction(transaction_id: TransactionId) -> dict:
    """Fetch a PayPal transaction (capture) by its 17-character ID.

    Returns status (e.g. COMPLETED, PARTIALLY_REFUNDED), amount, and times.
    """
    with session_scope() as session:
        _authenticate(session)
    try:
        capture = _get_paypal().get_capture(transaction_id)
    except PayPalError as exc:
        raise ToolError(f"PAYPAL_ERROR: {_describe_paypal_error(exc)}") from exc

    amount = _money(capture.get("amount"))
    return {
        "ok": True,
        "summary": (f"Transaction {capture.get('id')}: {amount['value']} "
                    f"{amount['currency']} — status {capture.get('status')}."),
        "transaction": {
            "id": capture.get("id"),
            "status": capture.get("status"),
            "amount": amount,
            "final_capture": capture.get("final_capture"),
            "disbursement_mode": capture.get("disbursement_mode"),
            "created_time": capture.get("create_time"),
            "updated_time": capture.get("update_time"),
        },
    }


@mcp.tool()
def list_disputes(
    page_size: Annotated[int, Field(ge=1, le=50, description="Items per page (1-50).")] = 20,
    start_time: StartTime | None = None,
) -> dict:
    """List PayPal disputes for the business account, newest first.

    Optionally filter to disputes created at/after start_time (RFC 3339 UTC).
    """
    with session_scope() as session:
        _authenticate(session)
    try:
        page = _get_paypal().list_disputes(page_size=page_size, start_time=start_time)
    except PayPalError as exc:
        raise ToolError(f"PAYPAL_ERROR: {_describe_paypal_error(exc)}") from exc

    items = page.get("items", [])
    disputes = [
        {
            "id": it.get("dispute_id"),
            "reason": it.get("reason"),
            "status": it.get("status"),
            "dispute_amount": _money(it.get("dispute_amount")),
            "create_time": it.get("create_time"),
        }
        for it in items
    ]
    return {
        "ok": True,
        "summary": f"{len(disputes)} dispute(s) returned (page_size={page_size}).",
        "disputes": disputes,
        "total_items": page.get("total_items"),
    }


@mcp.tool()
def get_dispute(dispute_id: DisputeId) -> dict:
    """Fetch one PayPal dispute by ID (e.g. PP-D-56244), with full detail."""
    with session_scope() as session:
        _authenticate(session)
    try:
        dispute = _get_paypal().get_dispute(dispute_id)
    except PayPalError as exc:
        raise ToolError(f"PAYPAL_ERROR: {_describe_paypal_error(exc)}") from exc

    return {
        "ok": True,
        "summary": (f"Dispute {dispute.get('dispute_id')}: "
                    f"{dispute.get('reason')} — status {dispute.get('status')}, "
                    f"{_money(dispute.get('dispute_amount'))['value']} "
                    f"{_money(dispute.get('dispute_amount'))['currency']}."),
        "dispute": dispute,
    }


# ---------------------------------------------------------------------------
# Tools: money-moving (identity + rate limit + FULL policy engine)
# ---------------------------------------------------------------------------

@mcp.tool()
def issue_refund(
    capture_id: TransactionId,
    amount: Amount,
    idempotency_key: IdempotencyKey,
    currency: Currency = "USD",
    note_to_payer: Annotated[str | None, Field(max_length=256)] = None,
) -> dict:
    """Refund a captured payment — gated by this agent's policy.

    Pipeline: allowlist → per-txn cap → rolling 24h cap → approval threshold.
    Outcomes: 'allowed' executes the refund on PayPal immediately;
    'pending_approval' creates an approval a human must decide (no money
    moves); 'denied'/'deduplicated' move nothing. Replaying the same
    idempotency_key is always safe.
    """
    with session_scope() as session:
        agent, policy = _authenticate(session)
        decision = authorize(session, agent, policy, EngineRequest(
            action_type="refund",
            amount=amount,
            currency=currency,
            idempotency_key=idempotency_key,
            reference_id=capture_id,
        ))
        session.commit()  # commit before you speak: the decision is real

        if decision.outcome != "allowed":
            return _not_allowed_envelope(decision, amount, currency, "Refund")

        # Allowed → execute on PayPal. Deterministic request id per action:
        # a retry after timeout re-sends the SAME PayPal-Request-Id, so PayPal
        # returns the stored result instead of refunding twice.
        request_id = str(uuid.uuid5(
            _PAYPAL_REQUEST_NS, f"tollgate-refund-action-{decision.action_id}"))
        try:
            refund = _get_paypal().refund_capture(
                capture_id, amount, currency,
                note_to_payer=note_to_payer, request_id=request_id)
        except PayPalError as exc:
            audit.record(session, decision="failed",
                         reason_code="PAYPAL_REFUND_FAILED",
                         agent_id=agent.id, action_id=decision.action_id,
                         amount=amount,
                         detail={"capture_id": capture_id,
                                 "paypal_request_id": request_id,
                                 "error": _describe_paypal_error(exc)[:300]})
            _emit(session, event_type="refund.failed", aggregate_type="action",
                  aggregate_id=decision.action_id,
                  payload={"capture_id": capture_id,
                           "paypal_request_id": request_id})
            session.commit()  # commit the evidence before raising
            raise ToolError(f"PAYPAL_ERROR: {_describe_paypal_error(exc)}") from exc

        refund_amount = _money(refund.get("amount"))
        audit.record(session, decision="executed",
                     reason_code="PAYPAL_REFUND_COMPLETED",
                     agent_id=agent.id, action_id=decision.action_id,
                     amount=amount,
                     detail={"capture_id": capture_id,
                             "refund_id": refund.get("id"),
                             "paypal_request_id": request_id})
        _emit(session, event_type="refund.executed", aggregate_type="action",
              aggregate_id=decision.action_id,
              payload={"capture_id": capture_id, "refund_id": refund.get("id")})
        session.commit()  # commit before you speak

    return {
        "ok": True,
        "summary": (f"Refund {refund.get('id')} executed: {refund_amount['value']} "
                    f"{refund_amount['currency']} against capture {capture_id} "
                    f"(status {refund.get('status')})."),
        "decision": _decision_payload(decision),
        "refund": {
            "id": refund.get("id"),
            "status": refund.get("status"),
            "amount": refund_amount,
            "paypal_request_id": request_id,
        },
    }


@mcp.tool()
def execute_approved_refund(
    approval_id: Annotated[int, Field(gt=0, description="Approval id returned by issue_refund's pending_approval outcome.")],
) -> dict:
    """Execute a refund a human has approved (approval status 'approved').

    This is the second half of the pending_approval path: issue_refund parks
    the money, a human decides via the approvals API, and this tool performs
    the actual PayPal refund and records the result (approved → executed |
    failed) through the same locked, audited state-machine transition as
    every other approval change.

    Idempotent: replaying on an already-executed approval returns the
    recorded outcome without calling PayPal again, and the PayPal request id
    is deterministic per action, so even a crash between the PayPal call and
    the status write cannot double-refund on retry.

    Known simplification (same as the HTTP /execute endpoint): the caller
    authenticates with an agent key — a separate operator identity is on the
    patch list.
    """
    with session_scope() as session:
        agent, policy = _authenticate(session)
        approval = session.execute(
            select(Approval).where(Approval.id == approval_id).with_for_update()
        ).scalar_one_or_none()
        # Ownership: cross-agent probes get NOT_FOUND (do not confirm existence).
        if approval is None or approval.action.agent_id != agent.id:
            raise ToolError(f"APPROVAL_NOT_FOUND: no approval #{approval_id} for this agent")

        if approval.status == "executed":
            # Idempotent replay: already done — report without touching PayPal.
            return {
                "ok": True, "already_executed": True,
                "summary": f"Approval #{approval_id} was already executed.",
                "approval": {"id": approval.id, "status": "executed",
                             "reason_code": approval.reason_code},
            }
        if approval.status != "approved":
            raise ToolError(
                f"INVALID_STATE: approval #{approval_id} is '{approval.status}'; "
                f"only 'approved' refunds can be executed")

        action = approval.action
        request_id = str(uuid.uuid5(
            _PAYPAL_REQUEST_NS, f"tollgate-refund-action-{action.id}"))
        try:
            refund = _get_paypal().refund_capture(
                action.reference_id, action.amount, action.currency,
                request_id=request_id)
        except PayPalError as exc:
            approvals_mod.mark_execution(
                session, approval.id, executed=False,
                reason_code="PAYPAL_REFUND_FAILED",
                detail={"capture_id": action.reference_id,
                        "paypal_request_id": request_id,
                        "error": _describe_paypal_error(exc)[:300]})
            session.commit()  # commit the evidence before raising
            raise ToolError(f"PAYPAL_ERROR: {_describe_paypal_error(exc)}") from exc

        refund_amount = _money(refund.get("amount"))
        approvals_mod.mark_execution(
            session, approval.id, executed=True,
            reason_code="PAYPAL_REFUND_COMPLETED",
            detail={"capture_id": action.reference_id,
                    "refund_id": refund.get("id"),
                    "paypal_request_id": request_id})
        session.commit()  # commit before you speak

    return {
        "ok": True,
        "summary": (f"Approved refund executed: {refund.get('id')} "
                    f"{refund_amount['value']} {refund_amount['currency']} "
                    f"against capture {action.reference_id} "
                    f"(status {refund.get('status')})."),
        "approval": {"id": approval.id, "status": "executed",
                     "reason_code": "PAYPAL_REFUND_COMPLETED"},
        "refund": {
            "id": refund.get("id"),
            "status": refund.get("status"),
            "amount": refund_amount,
            "paypal_request_id": request_id,
        },
    }


@mcp.tool()
def create_order(
    amount: Amount,
    idempotency_key: IdempotencyKey,
    currency: Currency = "USD",
    description: Annotated[str | None, Field(max_length=127)] = None,
) -> dict:
    """Create a PayPal order (intent=CAPTURE) — gated by this agent's policy.

    No money moves at creation; the buyer approves via the returned URL and
    the order is captured later. If the amount hits the approval threshold,
    the ORDER ITSELF waits for a human first (pending_approval, no PayPal
    call). Requires 'create_order' in the agent's allowed_actions.
    """
    with session_scope() as session:
        agent, policy = _authenticate(session)
        decision = authorize(session, agent, policy, EngineRequest(
            action_type="create_order",
            amount=amount,
            currency=currency,
            idempotency_key=idempotency_key,
            reference_id=None,
        ))
        session.commit()  # commit before you speak

        if decision.outcome != "allowed":
            return _not_allowed_envelope(decision, amount, currency, "Order creation")

        request_id = str(uuid.uuid5(
            _PAYPAL_REQUEST_NS, f"tollgate-order-action-{decision.action_id}"))
        try:
            order = _get_paypal().create_order(
                amount, currency, description=description,
                custom_id=f"agent-{agent.id}-action-{decision.action_id}",
                request_id=request_id)
        except PayPalError as exc:
            audit.record(session, decision="failed",
                         reason_code="PAYPAL_ORDER_FAILED",
                         agent_id=agent.id, action_id=decision.action_id,
                         amount=amount,
                         detail={"paypal_request_id": request_id,
                                 "error": _describe_paypal_error(exc)[:300]})
            _emit(session, event_type="order.failed", aggregate_type="action",
                  aggregate_id=decision.action_id,
                  payload={"paypal_request_id": request_id})
            session.commit()
            raise ToolError(f"PAYPAL_ERROR: {_describe_paypal_error(exc)}") from exc

        audit.record(session, decision="executed",
                     reason_code="PAYPAL_ORDER_CREATED",
                     agent_id=agent.id, action_id=decision.action_id,
                     amount=amount,
                     detail={"order_id": order.get("id"),
                             "paypal_request_id": request_id})
        _emit(session, event_type="order.created", aggregate_type="action",
              aggregate_id=decision.action_id, payload={"order_id": order.get("id")})
        session.commit()  # commit before you speak

    approve_url = get_approval_url(order)
    return {
        "ok": True,
        "summary": (f"Order {order.get('id')} created for {amount} {currency} "
                    f"(status {order.get('status')}). Buyer approval URL: "
                    f"{approve_url or 'n/a'}."),
        "decision": _decision_payload(decision),
        "order": {
            "id": order.get("id"),
            "status": order.get("status"),
            "amount": {"value": str(amount), "currency": currency},
            "approve_url": approve_url,
            "paypal_request_id": request_id,
        },
    }
