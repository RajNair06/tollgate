"""MCP server tests: real streamable-HTTP server, real PostgreSQL, mocked PayPal.

Topology under test is identical to main.py's wiring: a FastAPI app mounts
mcp.http_app(path="/mcp", json_response=True) and runs mcp.lifespan() inside
its own lifespan. The only substitution is the PayPal client (MockTransport),
injected through the same init_paypal_client() seam main.py uses — and each
test re-injects a fresh mock so it can observe (or forbid) outbound calls.

Auth is exercised over the wire: the fastmcp Client sends the agent's API key
as an Authorization header on every HTTP request, exactly like a real MCP
client would.
"""

import asyncio
import contextlib
import json
import threading
import time
from decimal import Decimal

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from sqlalchemy import select, text

from gateway.db import session_scope
from gateway.mcp_server import init_paypal_client, mcp
from gateway.models import Action, Approval, AuditLog
from gateway.paypal_client import PayPalClient

CAPTURE_ID = "05Y454980K6595141"  # 17 chars, real sandbox shape
DISPUTE_ID = "PP-D-56244"


# ---------------------------------------------------------------------------
# Mock PayPal
# ---------------------------------------------------------------------------

def _mock_paypal(calls: list[httpx.Request]) -> PayPalClient:
    """PayPalClient with a MockTransport that records every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(
                200, json={"access_token": "mock-token", "expires_in": 32400})
        if path.startswith("/v2/payments/captures/") and path.endswith("/refund"):
            body = json.loads(request.content) if request.content else {}
            amount = body.get("amount", {"value": "10.00", "currency_code": "USD"})
            return httpx.Response(201, json={
                "id": "2VF20713Y24658739", "status": "COMPLETED",
                "amount": amount,
                "links": [{"rel": "up", "href": f"https://api/v2/payments/captures/{path.split('/')[4]}"}],
            })
        if path.startswith("/v2/payments/captures/"):
            return httpx.Response(200, json={
                "id": CAPTURE_ID, "status": "PARTIALLY_REFUNDED",
                "amount": {"value": "12.99", "currency_code": "USD"},
                "final_capture": False, "disbursement_mode": "INSTANT",
                "create_time": "2026-10-01T10:00:00Z",
                "update_time": "2026-10-02T11:00:00Z",
            })
        if path == "/v1/customer/disputes":
            return httpx.Response(200, json={
                "items": [{
                    "dispute_id": DISPUTE_ID, "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                    "status": "WAITING_FOR_BUYER_RESPONSE",
                    "dispute_amount": {"value": "5.00", "currency_code": "USD"},
                    "create_time": "2026-10-03T09:00:00Z",
                }],
                "total_items": 1,
            })
        if path.startswith("/v1/customer/disputes/"):
            return httpx.Response(200, json={
                "dispute_id": DISPUTE_ID, "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                "status": "WAITING_FOR_BUYER_RESPONSE",
                "dispute_amount": {"value": "5.00", "currency_code": "USD"},
                "create_time": "2026-10-03T09:00:00Z",
                "disputed_transactions": [{"seller_transaction_id": CAPTURE_ID}],
            })
        if path == "/v2/checkout/orders":
            return httpx.Response(201, json={
                "id": "5O7122185R666592F", "status": "CREATED",
                "links": [{"rel": "payer-action",
                           "href": "https://sandbox.paypal.com/checkoutnow?token=5O7122185R666592F"}],
            })
        return httpx.Response(404, json={
            "name": "RESOURCE_NOT_FOUND", "message": "unknown path",
            "debug_id": "mock-debug-id"})

    return PayPalClient("id", "secret", "https://mock.paypal.invalid",
                        transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Server fixture: uvicorn thread, module-scoped, same wiring as main.py
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _mcp_server_url():
    calls: list[httpx.Request] = []
    init_paypal_client(_mock_paypal(calls))

    mcp_app = mcp.http_app(path="/mcp", json_response=True)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp_app.lifespan(app):  # same hook main.py uses
            yield

    app = FastAPI(lifespan=lifespan)
    app.mount("/", mcp_app)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("MCP test server did not start")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp", calls
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="module")
def server():
    with _mcp_server_url() as (url, calls):
        yield url, calls


@pytest.fixture(autouse=True)
def fresh_mock(server):
    """Each test gets an empty PayPal call recorder (and a live mock)."""
    url, calls = server
    calls.clear()
    init_paypal_client(_mock_paypal(calls))
    return calls


# ---------------------------------------------------------------------------
# Client helpers (sync wrappers over the async fastmcp Client)
# ---------------------------------------------------------------------------

def _client(url: str, key: str | None) -> Client:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return Client(StreamableHttpTransport(url=url, headers=headers))


async def _call(url, key, tool, arguments):
    async with _client(url, key) as client:
        return await client.call_tool(tool, arguments)


async def _list_tools(url, key):
    async with _client(url, key) as client:
        return await client.list_tools()


def call_tool(server, key, tool, arguments):
    url, _ = server
    return asyncio.run(_call(url, key, tool, arguments))


def _paypal_paths(calls: list[httpx.Request]) -> list[str]:
    return [c.url.path for c in calls if c.url.path != "/v1/oauth2/token"]


def _latest_audit(reason_code: str) -> AuditLog | None:
    with session_scope() as s:
        return s.execute(
            select(AuditLog).where(AuditLog.reason_code == reason_code)
            .order_by(AuditLog.id.desc()).limit(1)
        ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_all_six_tools_are_listed(server, make_agent):
    _, key = make_agent()
    url, _ = server
    tools = asyncio.run(_list_tools(url, key))
    assert {t.name for t in tools} == {
        "get_transaction", "list_disputes", "get_dispute",
        "issue_refund", "create_order", "execute_approved_refund",
    }


# ---------------------------------------------------------------------------
# Authentication (checklist: unauthorized requests)
# ---------------------------------------------------------------------------

def test_missing_key_rejected_and_audited(server):
    with pytest.raises(ToolError) as exc:
        call_tool(server, None, "get_transaction", {"transaction_id": CAPTURE_ID})
    assert "MISSING_API_KEY" in str(exc.value)
    row = _latest_audit("MISSING_API_KEY")
    assert row is not None and row.decision == "auth_failed"
    assert row.detail.get("via") == "mcp"


def test_invalid_key_rejected_and_audited(server):
    with pytest.raises(ToolError) as exc:
        call_tool(server, "tg_" + "0" * 48, "list_disputes", {})
    assert "INVALID_API_KEY" in str(exc.value)
    row = _latest_audit("INVALID_API_KEY")
    assert row is not None
    # The plaintext key must never reach the audit row — prefix only.
    assert row.detail.get("key_prefix") == "tg_" + "0" * 8


def test_revoked_key_rejected(server, make_agent):
    from gateway.api_keys import hash_api_key
    from gateway.models import AgentKey

    from sqlalchemy import func

    _, key = make_agent()
    with session_scope() as s:
        row = s.execute(
            select(AgentKey).where(AgentKey.key_hash == hash_api_key(key))
        ).scalar_one()
        row.revoked_at = func.now()
    with pytest.raises(ToolError) as exc:
        call_tool(server, key, "get_transaction", {"transaction_id": CAPTURE_ID})
    assert "API_KEY_REVOKED" in str(exc.value)


# ---------------------------------------------------------------------------
# Validation (checklist: invalid parameters)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_args,needle", [
    ({"transaction_id": "abc"}, "transaction_id"),              # wrong format
    ({"transaction_id": "05Y454980K659514"}, "transaction_id"),  # 16 chars
    ({}, "transaction_id"),                                     # missing
])
def test_get_transaction_rejects_bad_ids(server, make_agent, bad_args, needle):
    _, key = make_agent()
    with pytest.raises(ToolError) as exc:
        call_tool(server, key, "get_transaction", bad_args)
    assert needle in str(exc.value)


def test_refund_rejects_bad_parameters(server, make_agent, fresh_mock):
    _, key = make_agent()
    base = {"capture_id": CAPTURE_ID, "amount": "10.00", "idempotency_key": "key-0001"}

    with pytest.raises(ToolError):  # negative amount
        call_tool(server, key, "issue_refund", {**base, "amount": "-5.00",
                                                "idempotency_key": "key-0002"})
    with pytest.raises(ToolError):  # zero amount
        call_tool(server, key, "issue_refund", {**base, "amount": "0.00",
                                                "idempotency_key": "key-0003"})
    with pytest.raises(ToolError):  # JSON number, not decimal string
        call_tool(server, key, "issue_refund", {**base, "amount": 10.99,
                                                "idempotency_key": "key-0004"})
    with pytest.raises(ToolError):  # three decimal places
        call_tool(server, key, "issue_refund", {**base, "amount": "10.999",
                                                "idempotency_key": "key-0005"})
    with pytest.raises(ToolError):  # currency outside allowlist
        call_tool(server, key, "issue_refund", {**base, "currency": "ZZZ",
                                                "idempotency_key": "key-0006"})
    with pytest.raises(ToolError):  # short idempotency key
        call_tool(server, key, "issue_refund", {**base, "idempotency_key": "abc"})
    assert _paypal_paths(fresh_mock) == []  # nothing leaked through to PayPal


def test_dispute_id_format_enforced(server, make_agent):
    _, key = make_agent()
    with pytest.raises(ToolError):
        call_tool(server, key, "get_dispute", {"dispute_id": "not-a-dispute"})


# ---------------------------------------------------------------------------
# Policy enforcement through the engine (checklist: denied / pending)
# ---------------------------------------------------------------------------

def test_refund_denied_over_per_txn_cap(server, make_agent, fresh_mock):
    # per_txn cap $25 → a $30 refund is denied by the engine.
    _, key = make_agent(per_txn="25.00", rolling="500.00", threshold="25.00")
    result = call_tool(server, key, "issue_refund", {
        "capture_id": CAPTURE_ID, "amount": "30.00",
        "idempotency_key": "deny-0001"})
    payload = result.data
    assert payload["ok"] is True           # a denial is a result, not an error
    assert payload["decision"]["outcome"] == "denied"
    assert payload["decision"]["reason_code"] == "PER_TXN_CAP_EXCEEDED"
    assert "DENIED" in payload["summary"]
    assert _paypal_paths(fresh_mock) == []  # no money moved


def test_refund_denied_over_rolling_cap(server, make_agent, fresh_mock):
    # rolling cap $15 → a $10 refund is fine alone but $15 spent already...
    _, key = make_agent(per_txn="100.00", rolling="15.00", threshold="50.00")
    first = call_tool(server, key, "issue_refund", {
        "capture_id": CAPTURE_ID, "amount": "10.00",
        "idempotency_key": "roll-0001"})
    assert first.data["decision"]["outcome"] == "allowed"
    second = call_tool(server, key, "issue_refund", {
        "capture_id": CAPTURE_ID, "amount": "10.00",
        "idempotency_key": "roll-0002"})
    assert second.data["decision"]["outcome"] == "denied"
    assert second.data["decision"]["reason_code"] == "ROLLING_24H_CAP_EXCEEDED"


def test_refund_pending_creates_approval_and_reserves_cap(server, make_agent,
                                                          fresh_mock):
    # threshold $20 → a $22 refund waits for a human; PayPal is not called.
    _, key = make_agent(per_txn="100.00", rolling="500.00", threshold="20.00")
    result = call_tool(server, key, "issue_refund", {
        "capture_id": CAPTURE_ID, "amount": "22.00",
        "idempotency_key": "pend-0001"})
    payload = result.data
    assert payload["decision"]["outcome"] == "pending_approval"
    approval_id = payload["decision"]["approval_id"]
    assert "approval" in payload["summary"]
    assert _paypal_paths(fresh_mock) == []

    with session_scope() as s:
        approval = s.get(Approval, approval_id)
        assert approval.status == "pending"
        assert approval.action.amount == Decimal("22.00")
        assert approval.action.reference_id == CAPTURE_ID

    # The pending amount reserves rolling headroom: the next decision's
    # reported usage includes the $22 pending reservation (plus its own $1).
    probe = call_tool(server, key, "issue_refund", {
        "capture_id": CAPTURE_ID, "amount": "1.00",
        "idempotency_key": "pend-0002"})
    assert probe.data["decision"]["rolling_24h_used"] == "23.00"


def test_refund_replay_is_deduplicated(server, make_agent, fresh_mock):
    _, key = make_agent(per_txn="25.00", rolling="500.00", threshold="25.00")
    args = {"capture_id": CAPTURE_ID, "amount": "30.00",
            "idempotency_key": "dupe-0001"}
    first = call_tool(server, key, "issue_refund", args)
    assert first.data["decision"]["outcome"] == "denied"
    second = call_tool(server, key, "issue_refund", args)
    assert second.data["decision"]["outcome"] == "deduplicated"
    assert second.data["decision"]["original_outcome"] == "denied"
    assert _paypal_paths(fresh_mock) == []

    with session_scope() as s:
        rows = s.execute(select(Action).where(
            Action.idempotency_key == "dupe-0001")).scalars().all()
        assert len(rows) == 2
        replay = next(r for r in rows if r.outcome == "deduplicated")
        assert replay.duplicate_of_action_id is not None


def test_refund_allowed_executes_on_paypal_with_audit_trail(server, make_agent,
                                                            fresh_mock):
    _, key = make_agent(per_txn="100.00", rolling="500.00", threshold="50.00")
    result = call_tool(server, key, "issue_refund", {
        "capture_id": CAPTURE_ID, "amount": "10.00",
        "idempotency_key": "exec-0001", "note_to_payer": "damaged item"})
    payload = result.data
    assert payload["decision"]["outcome"] == "allowed"
    assert payload["refund"]["id"] == "2VF20713Y24658739"
    assert payload["refund"]["status"] == "COMPLETED"
    assert "executed" in payload["summary"].lower() or "refund" in payload["summary"].lower()

    refund_calls = [c for c in fresh_mock if c.url.path.endswith("/refund")]
    assert len(refund_calls) == 1
    # Deterministic PayPal-Request-Id (uuid5 of the action id) — replay-safe.
    import uuid
    from gateway.mcp_server import _PAYPAL_REQUEST_NS
    action_id = payload["decision"]["action_id"]
    expected = str(uuid.uuid5(_PAYPAL_REQUEST_NS,
                              f"tollgate-refund-action-{action_id}"))
    assert refund_calls[0].headers["PayPal-Request-Id"] == expected
    assert payload["refund"]["paypal_request_id"] == expected
    body = json.loads(refund_calls[0].content)
    assert body["amount"] == {"value": "10.00", "currency_code": "USD"}
    assert body["note_to_payer"] == "damaged item"

    row = _latest_audit("PAYPAL_REFUND_COMPLETED")
    assert row is not None and row.decision == "executed"
    assert row.detail["refund_id"] == "2VF20713Y24658739"
    assert row.amount == Decimal("10.00")


def test_paypal_failure_is_audited_as_failed(server, make_agent):
    """A PayPal 404 on the refund → ToolError + 'failed' audit row."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={
                "access_token": "t", "expires_in": 32400})
        return httpx.Response(404, json={
            "name": "RESOURCE_NOT_FOUND",
            "message": "The specified resource does not exist.",
            "debug_id": "deadbeef1234"})

    init_paypal_client(PayPalClient(
        "id", "secret", "https://mock.paypal.invalid",
        transport=httpx.MockTransport(handler)))

    _, key = make_agent(per_txn="100.00", rolling="500.00", threshold="50.00")
    with pytest.raises(ToolError) as exc:
        call_tool(server, key, "issue_refund", {
            "capture_id": CAPTURE_ID, "amount": "10.00",
            "idempotency_key": "fail-0001"})
    assert "RESOURCE_NOT_FOUND" in str(exc.value)
    assert "deadbeef1234" in str(exc.value)  # debug_id surfaced for support

    row = _latest_audit("PAYPAL_REFUND_FAILED")
    assert row is not None and row.decision == "failed"


# ---------------------------------------------------------------------------
# execute_approved_refund: the human-approval execution glue
# ---------------------------------------------------------------------------

def _make_pending_approval(key, server, idem="appr-0001", amount="22.00"):
    """issue_refund above threshold → pending; returns (approval_id, action_id)."""
    result = call_tool(server, key, "issue_refund", {
        "capture_id": CAPTURE_ID, "amount": amount, "idempotency_key": idem})
    d = result.data["decision"]
    assert d["outcome"] == "pending_approval"
    return d["approval_id"], d["action_id"]


def _human_approve(approval_id: int):
    from gateway import approvals as approvals_mod
    with session_scope() as s:
        approvals_mod.decide(s, approval_id, "approved", "ops-alice")


def test_execute_approved_refund_happy_path(server, make_agent, fresh_mock):
    _, key = make_agent(per_txn="100.00", rolling="500.00", threshold="20.00")
    approval_id, action_id = _make_pending_approval(key, server)
    _human_approve(approval_id)

    result = call_tool(server, key, "execute_approved_refund",
                       {"approval_id": approval_id})
    payload = result.data
    assert payload["ok"] is True
    assert payload["refund"]["id"] == "2VF20713Y24658739"
    assert payload["approval"]["status"] == "executed"

    # Deterministic request id — the SAME scheme as issue_refund uses.
    import uuid
    from gateway.mcp_server import _PAYPAL_REQUEST_NS
    expected = str(uuid.uuid5(_PAYPAL_REQUEST_NS,
                              f"tollgate-refund-action-{action_id}"))
    refund_calls = [c for c in fresh_mock if c.url.path.endswith("/refund")]
    assert refund_calls[0].headers["PayPal-Request-Id"] == expected
    # The refund used the action's stored amount, not a fresh argument.
    assert json.loads(refund_calls[0].content)["amount"] == {
        "value": "22.00", "currency_code": "USD"}

    with session_scope() as s:
        assert s.get(Approval, approval_id).status == "executed"
    row = _latest_audit("PAYPAL_REFUND_COMPLETED")
    assert row.decision == "executed"
    assert row.detail["refund_id"] == "2VF20713Y24658739"
    assert row.detail["approval_id"] == approval_id


def test_execute_replay_is_idempotent(server, make_agent, fresh_mock):
    _, key = make_agent(per_txn="100.00", rolling="500.00", threshold="20.00")
    approval_id, _ = _make_pending_approval(key, server, idem="appr-0002")
    _human_approve(approval_id)
    first = call_tool(server, key, "execute_approved_refund",
                      {"approval_id": approval_id})
    assert first.data["ok"] is True
    paypal_calls_after_first = len([c for c in fresh_mock
                                    if c.url.path.endswith("/refund")])

    second = call_tool(server, key, "execute_approved_refund",
                       {"approval_id": approval_id})
    assert second.data["already_executed"] is True
    # PayPal was NOT called a second time.
    assert len([c for c in fresh_mock
                if c.url.path.endswith("/refund")]) == paypal_calls_after_first


def test_execute_requires_approved_state(server, make_agent, fresh_mock):
    _, key = make_agent(per_txn="100.00", rolling="500.00", threshold="20.00")
    approval_id, _ = _make_pending_approval(key, server, idem="appr-0003")
    # Still pending — nobody decided yet.
    with pytest.raises(ToolError) as exc:
        call_tool(server, key, "execute_approved_refund",
                  {"approval_id": approval_id})
    assert "INVALID_STATE" in str(exc.value)
    assert _paypal_paths(fresh_mock) == []


def test_execute_rejects_other_agents_approval(server, make_agent, fresh_mock):
    _, key_a = make_agent(per_txn="100.00", rolling="500.00", threshold="20.00")
    _, key_b = make_agent(per_txn="100.00", rolling="500.00", threshold="20.00")
    approval_id, _ = _make_pending_approval(key_a, server, idem="appr-0004")
    _human_approve(approval_id)
    with pytest.raises(ToolError) as exc:
        call_tool(server, key_b, "execute_approved_refund",
                  {"approval_id": approval_id})
    assert "APPROVAL_NOT_FOUND" in str(exc.value)  # existence not confirmed
    assert _paypal_paths(fresh_mock) == []


def test_execute_paypal_failure_marks_approval_failed(server, make_agent):
    _, key = make_agent(per_txn="100.00", rolling="500.00", threshold="20.00")
    approval_id, _ = _make_pending_approval(key, server, idem="appr-0005")
    _human_approve(approval_id)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={
                "access_token": "t", "expires_in": 32400})
        return httpx.Response(500, json={
            "name": "INTERNAL_SERVER_ERROR", "message": "paypal broke",
            "debug_id": "fail-debug-9"})

    init_paypal_client(PayPalClient(
        "id", "secret", "https://mock.paypal.invalid",
        transport=httpx.MockTransport(handler)))

    with pytest.raises(ToolError) as exc:
        call_tool(server, key, "execute_approved_refund",
                  {"approval_id": approval_id})
    assert "fail-debug-9" in str(exc.value)

    with session_scope() as s:
        assert s.get(Approval, approval_id).status == "failed"
    row = _latest_audit("PAYPAL_REFUND_FAILED")
    assert row.decision == "failed"
    assert row.detail["approval_id"] == approval_id


# ---------------------------------------------------------------------------
# Rolling-cap release: definite vs ambiguous execution failures
# ---------------------------------------------------------------------------

def _failing_paypal(mode: str, calls: list[httpx.Request] | None = None) -> PayPalClient:
    """Mock PayPal whose refund endpoint fails.

    mode: "422" (definite business rejection), "500" (ambiguous server
    error), "timeout" (ambiguous transport failure).
    """
    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={
                "access_token": "t", "expires_in": 32400})
        if request.url.path.endswith("/refund"):
            if mode == "timeout":
                raise httpx.ConnectTimeout("connection timed out")
            status = int(mode)
            return httpx.Response(status, json={
                "name": "INVALID_REQUEST" if status == 422 else "INTERNAL_ERROR",
                "message": "refund failed", "debug_id": f"dbg-{mode}"})
        return httpx.Response(200, json={
            "id": CAPTURE_ID, "status": "COMPLETED",
            "amount": {"value": "12.99", "currency_code": "USD"}})

    return PayPalClient("id", "secret", "https://mock.paypal.invalid",
                        transport=httpx.MockTransport(handler))


def _allowed_action(idem_key: str) -> Action:
    with session_scope() as s:
        return s.execute(select(Action).where(
            Action.idempotency_key == idem_key,
            Action.outcome == "allowed")).scalar_one()


def test_definite_paypal_failure_releases_rolling_cap(server, make_agent, fresh_mock):
    # Rolling cap 25: the second $20 refund fits ONLY if the failed first
    # one released its reservation (20 + 20 > 25).
    _, key = make_agent(per_txn="100.00", rolling="25.00", threshold="100.00")

    init_paypal_client(_failing_paypal("422", fresh_mock))
    with pytest.raises(ToolError) as exc:
        call_tool(server, key, "issue_refund", {
            "capture_id": CAPTURE_ID, "amount": "20.00",
            "idempotency_key": "rel-def-0001"})
    assert "dbg-422" in str(exc.value)

    # The failure was definite → stamped + audit says the cap was released.
    assert _allowed_action("rel-def-0001").execution_failed_at is not None
    audit_row = _latest_audit("PAYPAL_REFUND_FAILED")
    assert audit_row.detail["cap_reservation_released"] is True

    # Same amount again with a fresh key → allowed and executed.
    init_paypal_client(_mock_paypal(fresh_mock))
    result = call_tool(server, key, "issue_refund", {
        "capture_id": CAPTURE_ID, "amount": "20.00",
        "idempotency_key": "rel-def-0002"})
    d = result.data["decision"]
    assert d["outcome"] == "allowed"
    assert d["rolling_24h_used"] == "20.00"  # only the new refund counts
    assert result.data["refund"]["id"] == "2VF20713Y24658739"


@pytest.mark.parametrize("mode", ["500", "timeout"])
def test_ambiguous_paypal_failure_keeps_reservation(server, make_agent,
                                                    fresh_mock, mode):
    # The money MAY have moved (5xx / lost answer) → reservation stays, and
    # a second refund that would exceed the cap is denied.
    _, key = make_agent(per_txn="100.00", rolling="25.00", threshold="100.00")

    init_paypal_client(_failing_paypal(mode, fresh_mock))
    with pytest.raises(ToolError):
        call_tool(server, key, "issue_refund", {
            "capture_id": CAPTURE_ID, "amount": "20.00",
            "idempotency_key": f"rel-amb-{mode}"})

    assert _allowed_action(f"rel-amb-{mode}").execution_failed_at is None
    audit_row = _latest_audit("PAYPAL_REFUND_FAILED")
    assert audit_row.detail["cap_reservation_released"] is False

    init_paypal_client(_mock_paypal(fresh_mock))
    result = call_tool(server, key, "issue_refund", {
        "capture_id": CAPTURE_ID, "amount": "20.00",
        "idempotency_key": f"rel-amb-{mode}-2"})
    d = result.data["decision"]
    assert d["outcome"] == "denied"
    assert d["reason_code"] == "ROLLING_24H_CAP_EXCEEDED"
    assert d["rolling_24h_used"] == "20.00"  # the ambiguous one still counts
    # The denied refund never reached PayPal: only the failed attempt did.
    assert len([p for p in _paypal_paths(fresh_mock)
                if p.endswith("/refund")]) == 1


def test_replay_after_definite_failure_flags_execution_failed(server, make_agent,
                                                              fresh_mock):
    # Decisions replay, they do not re-execute: the same key returns the
    # dedup envelope — now carrying the execution-failure flag so the agent
    # knows to retry with a NEW key.
    _, key = make_agent(per_txn="100.00", rolling="25.00", threshold="100.00")

    init_paypal_client(_failing_paypal("422", fresh_mock))
    with pytest.raises(ToolError):
        call_tool(server, key, "issue_refund", {
            "capture_id": CAPTURE_ID, "amount": "20.00",
            "idempotency_key": "rel-replay-0001"})

    init_paypal_client(_mock_paypal(fresh_mock))
    result = call_tool(server, key, "issue_refund", {
        "capture_id": CAPTURE_ID, "amount": "20.00",
        "idempotency_key": "rel-replay-0001"})  # SAME key
    d = result.data["decision"]
    assert d["outcome"] == "deduplicated"
    assert d["original_outcome"] == "allowed"
    assert d["original_execution_failed"] is True
    assert "NEW idempotency key" in result.data["summary"]
    # The replay did not execute anything: still just the one failed attempt.
    assert len([p for p in _paypal_paths(fresh_mock)
                if p.endswith("/refund")]) == 1


def test_approved_execution_failure_releases_via_approval_status(server, make_agent,
                                                                 fresh_mock):
    # The pending path releases through the approvals join (status 'failed'
    # is not cap-reserving) — even for an AMBIGUOUS failure. Locked in so a
    # refactor cannot silently regress it. Threshold 21: $22 parks, $20 runs.
    _, key = make_agent(per_txn="100.00", rolling="25.00", threshold="21.00")

    pending = call_tool(server, key, "issue_refund", {
        "capture_id": CAPTURE_ID, "amount": "22.00",
        "idempotency_key": "rel-appr-0001"})
    approval_id = pending.data["decision"]["approval_id"]
    assert pending.data["decision"]["outcome"] == "pending_approval"
    _human_approve(approval_id)

    init_paypal_client(_failing_paypal("500", fresh_mock))
    with pytest.raises(ToolError):
        call_tool(server, key, "execute_approved_refund",
                  {"approval_id": approval_id})
    with session_scope() as s:
        assert s.get(Approval, approval_id).status == "failed"

    # 22 released → a $20 refund fits under the 25 cap and executes.
    init_paypal_client(_mock_paypal(fresh_mock))
    result = call_tool(server, key, "issue_refund", {
        "capture_id": CAPTURE_ID, "amount": "20.00",
        "idempotency_key": "rel-appr-0002"})
    d = result.data["decision"]
    assert d["outcome"] == "allowed"
    assert d["rolling_24h_used"] == "20.00"
    assert result.data["refund"]["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# create_order through the gateway + policy
# ---------------------------------------------------------------------------

def test_create_order_denied_when_not_in_allowlist(server, make_agent, fresh_mock):
    _, key = make_agent(allowed=("refund",))
    result = call_tool(server, key, "create_order", {
        "amount": "5.00", "idempotency_key": "ordr-0001"})
    assert result.data["decision"]["outcome"] == "denied"
    assert result.data["decision"]["reason_code"] == "ACTION_NOT_ALLOWED"
    assert _paypal_paths(fresh_mock) == []


def test_create_order_allowed_returns_approval_url(server, make_agent, fresh_mock):
    _, key = make_agent(per_txn="100.00", rolling="500.00", threshold="50.00",
                        allowed=("refund", "create_order"))
    result = call_tool(server, key, "create_order", {
        "amount": "5.00", "idempotency_key": "ordr-0002",
        "description": "test order"})
    payload = result.data
    assert payload["decision"]["outcome"] == "allowed"
    assert payload["order"]["id"] == "5O7122185R666592F"
    assert "sandbox.paypal.com" in payload["order"]["approve_url"]
    assert len(_paypal_paths(fresh_mock)) == 1
    assert _latest_audit("PAYPAL_ORDER_CREATED") is not None


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------

def test_get_transaction_returns_structured_result(server, make_agent, fresh_mock):
    _, key = make_agent()
    result = call_tool(server, key, "get_transaction",
                       {"transaction_id": CAPTURE_ID})
    payload = result.data
    assert payload["ok"] is True
    assert payload["transaction"]["status"] == "PARTIALLY_REFUNDED"
    assert payload["transaction"]["amount"] == {"value": "12.99", "currency": "USD"}
    assert CAPTURE_ID in payload["summary"]
    assert _paypal_paths(fresh_mock) == [f"/v2/payments/captures/{CAPTURE_ID}"]


def test_list_disputes_and_get_dispute(server, make_agent, fresh_mock):
    _, key = make_agent()
    listing = call_tool(server, key, "list_disputes", {"page_size": 5}).data
    assert listing["total_items"] == 1
    assert listing["disputes"][0]["id"] == DISPUTE_ID
    assert "1 dispute" in listing["summary"]

    detail = call_tool(server, key, "get_dispute",
                       {"dispute_id": DISPUTE_ID}).data
    assert detail["dispute"]["reason"] == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
    assert DISPUTE_ID in detail["summary"]
    assert _paypal_paths(fresh_mock) == [
        "/v1/customer/disputes", f"/v1/customer/disputes/{DISPUTE_ID}"]


def test_list_disputes_rejects_bad_page_size(server, make_agent):
    _, key = make_agent()
    with pytest.raises(ToolError):
        call_tool(server, key, "list_disputes", {"page_size": 999})
