"""Policy engine: the authorization decision for one agent action request.

Decision pipeline (in order — cheapest and most specific checks first):
  1. Dedup     — same (agent, idempotency_key) seen before? → deduplicated
  2. Allowlist — action_type not in policy.allowed_actions?  → denied
  3. Per-txn   — amount > per_txn_cap?                       → denied
  4. Rolling   — 24h consumed + amount > rolling_24h_cap?    → denied
  5. Threshold — amount >= approval_threshold?                → pending_approval
  6. Otherwise                                              → allowed

Atomicity of the rolling cap
----------------------------
Concurrent requests from one agent must not BOTH see headroom and together
blow through the cap (lost-update race). Two mechanisms, belt and braces:
  * SELECT ... FOR UPDATE on the agent row at the start of the transaction.
    Every concurrent authorize() for that agent queues on this row lock, so
    decisions are serialized per agent. Different agents never block each other.
  * The partial unique index on (agent_id, idempotency_key) as a hard
    backstop against duplicate live actions even in exotic races.

What counts toward the rolling cap:
  * outcome='allowed' actions (money committed), plus
  * outcome='pending_approval' actions whose approval is still live
    (status pending/approved/executed). Rejected/expired/failed approvals
    RELEASE their reservation — the money never moved.

Everything the engine writes (action, approval, audit, event, outbox) lands
in the CALLER's session/transaction: all-or-nothing with the decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select, and_
from sqlalchemy.orm import Session

from gateway import audit
from gateway.models import Action, Agent, Approval, Event, Outbox, Policy

logger = logging.getLogger(__name__)

# Reason codes — the controlled vocabulary for decisions and audit rows.
RC_OK = "OK"
RC_DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
RC_ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
RC_PER_TXN_CAP_EXCEEDED = "PER_TXN_CAP_EXCEEDED"
RC_ROLLING_24H_CAP_EXCEEDED = "ROLLING_24H_CAP_EXCEEDED"
RC_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
RC_AGENT_INACTIVE = "AGENT_INACTIVE"

# Approval statuses that still RESERVE their amount against the rolling cap.
_CAP_RESERVING_STATUSES = ("pending", "approved", "executed")


@dataclass(frozen=True)
class ActionRequest:
    """A validated agent action request (Pydantic layer converts to this)."""
    action_type: str
    amount: Decimal
    currency: str
    idempotency_key: str
    reference_id: str | None = None


@dataclass(frozen=True)
class Decision:
    """The engine's verdict, ready to serialize into an API response."""
    outcome: str                      # allowed | denied | pending_approval | deduplicated
    reason_code: str
    action_id: int
    approval_id: int | None = None
    duplicate_of_action_id: int | None = None
    original_outcome: str | None = None   # for deduplicated: the first decision
    rolling_24h_used: Decimal = Decimal("0")
    rolling_24h_cap: Decimal = Decimal("0")


def rolling_consumption(session: Session, agent_id: int) -> Decimal:
    """Sum of cap-reserving action amounts for this agent in the last 24h.

    Uses the DATABASE clock (now()) so every concurrent transaction measures
    the window identically — no app-server clock skew.
    """
    window_start = func.now() - timedelta(hours=24)
    total = session.execute(
        select(func.coalesce(func.sum(Action.amount), Decimal("0")))
        .outerjoin(Approval, Approval.action_id == Action.id)
        .where(
            Action.agent_id == agent_id,
            Action.created_at >= window_start,
            or_(
                Action.outcome == "allowed",
                and_(
                    Action.outcome == "pending_approval",
                    Approval.status.in_(_CAP_RESERVING_STATUSES),
                ),
            ),
        )
    ).scalar_one()
    return Decimal(total)


def _emit(session: Session, *, event_type: str, aggregate_type: str,
          aggregate_id: int, payload: dict) -> None:
    """Write the domain event AND its outbox row (same transaction).

    The outbox is the transactional-outbox pattern: the event is guaranteed
    to exist if and only if the decision committed. A separate relay process
    publishes outbox rows later; a crash between commit and publish loses
    nothing.
    """
    session.add(Event(event_type=event_type, aggregate_type=aggregate_type,
                      aggregate_id=aggregate_id, payload=payload))
    session.add(Outbox(event_type=event_type, aggregate_type=aggregate_type,
                       aggregate_id=aggregate_id, payload=payload))


def authorize(session: Session, agent: Agent, policy: Policy,
              req: ActionRequest) -> Decision:
    """Run the decision pipeline. Does NOT commit — caller owns the transaction."""
    if not agent.is_active:
        # No action row for an inactive agent (they must not accumulate
        # history under suspension); audit records the refusal.
        row = audit.record(session, decision="denied", reason_code=RC_AGENT_INACTIVE,
                           agent_id=agent.id, amount=req.amount,
                           detail={"action_type": req.action_type,
                                   "idempotency_key": req.idempotency_key})
        session.flush()
        logger.warning("Inactive agent %s attempted %s", agent.id, req.action_type)
        _ = row
        return Decision(outcome="denied", reason_code=RC_AGENT_INACTIVE, action_id=0)

    # -- Serialize per agent: the rolling-cap check + insert must be atomic.
    session.execute(select(Agent.id).where(Agent.id == agent.id).with_for_update())

    # -- 1. Dedup -------------------------------------------------------------
    existing = session.execute(
        select(Action).where(
            Action.agent_id == agent.id,
            Action.idempotency_key == req.idempotency_key,
            Action.outcome != "deduplicated",
        )
    ).scalar_one_or_none()

    if existing is not None:
        replay = Action(
            agent_id=agent.id, action_type=req.action_type, amount=req.amount,
            currency=req.currency, idempotency_key=req.idempotency_key,
            outcome="deduplicated", reason_code=RC_DUPLICATE_REQUEST,
            reference_id=req.reference_id, duplicate_of_action_id=existing.id,
        )
        session.add(replay)
        session.flush()  # assigns replay.id
        audit.record(session, decision="deduplicated", reason_code=RC_DUPLICATE_REQUEST,
                     agent_id=agent.id, action_id=replay.id, amount=req.amount,
                     detail={"duplicate_of_action_id": existing.id,
                             "original_outcome": existing.outcome})
        _emit(session, event_type="action.deduplicated", aggregate_type="action",
              aggregate_id=replay.id,
              payload={"duplicate_of_action_id": existing.id})
        logger.info("Duplicate request from agent %s (key=%s) → original action %s",
                    agent.id, req.idempotency_key, existing.id)
        return Decision(outcome="deduplicated", reason_code=RC_DUPLICATE_REQUEST,
                        action_id=replay.id, duplicate_of_action_id=existing.id,
                        original_outcome=existing.outcome)

    # -- 2. Allowlist ----------------------------------------------------------
    if req.action_type not in (policy.allowed_actions or []):
        return _deny(session, agent, policy, req, RC_ACTION_NOT_ALLOWED,
                     detail={"allowed_actions": policy.allowed_actions})

    # -- 3. Per-transaction cap -------------------------------------------------
    if req.amount > policy.per_txn_cap:
        return _deny(session, agent, policy, req, RC_PER_TXN_CAP_EXCEEDED,
                     detail={"per_txn_cap": str(policy.per_txn_cap)})

    # -- 4. Rolling 24h cap (atomic: agent row is locked above) -----------------
    consumed = rolling_consumption(session, agent.id)
    if consumed + req.amount > policy.rolling_24h_cap:
        return _deny(session, agent, policy, req, RC_ROLLING_24H_CAP_EXCEEDED,
                     detail={"rolling_24h_used": str(consumed),
                             "rolling_24h_cap": str(policy.rolling_24h_cap),
                             "attempted": str(req.amount)})

    # -- 5/6. Approval threshold → pending_approval, else allowed ---------------
    if req.amount >= policy.approval_threshold:
        return _pending(session, agent, policy, req, consumed)
    return _allow(session, agent, policy, req, consumed)


# ---------------------------------------------------------------------------
# Terminal builders — each writes action + audit + event/outbox in one go
# ---------------------------------------------------------------------------

def _new_action(agent: Agent, req: ActionRequest, outcome: str, reason: str) -> Action:
    return Action(
        agent_id=agent.id, action_type=req.action_type, amount=req.amount,
        currency=req.currency, idempotency_key=req.idempotency_key,
        outcome=outcome, reason_code=reason, reference_id=req.reference_id,
    )


def _deny(session: Session, agent: Agent, policy: Policy, req: ActionRequest,
          reason: str, detail: dict) -> Decision:
    action = _new_action(agent, req, "denied", reason)
    session.add(action)
    session.flush()
    consumed = rolling_consumption(session, agent.id)
    audit.record(session, decision="denied", reason_code=reason,
                 agent_id=agent.id, action_id=action.id, amount=req.amount,
                 detail={"action_type": req.action_type, **detail})
    _emit(session, event_type="action.denied", aggregate_type="action",
          aggregate_id=action.id, payload={"reason_code": reason})
    logger.info("DENIED agent=%s action=%s amount=%s reason=%s",
                agent.id, req.action_type, req.amount, reason)
    return Decision(outcome="denied", reason_code=reason, action_id=action.id,
                    rolling_24h_used=consumed, rolling_24h_cap=policy.rolling_24h_cap)


def _allow(session: Session, agent: Agent, policy: Policy, req: ActionRequest,
           consumed_before: Decimal) -> Decision:
    action = _new_action(agent, req, "allowed", RC_OK)
    session.add(action)
    session.flush()
    audit.record(session, decision="allowed", reason_code=RC_OK,
                 agent_id=agent.id, action_id=action.id, amount=req.amount,
                 detail={"action_type": req.action_type,
                         "rolling_24h_used_after": str(consumed_before + req.amount)})
    _emit(session, event_type="action.allowed", aggregate_type="action",
          aggregate_id=action.id, payload={"amount": str(req.amount)})
    logger.info("ALLOWED agent=%s action=%s amount=%s", agent.id, req.action_type, req.amount)
    return Decision(outcome="allowed", reason_code=RC_OK, action_id=action.id,
                    rolling_24h_used=consumed_before + req.amount,
                    rolling_24h_cap=policy.rolling_24h_cap)


def _pending(session: Session, agent: Agent, policy: Policy, req: ActionRequest,
             consumed_before: Decimal) -> Decision:
    action = _new_action(agent, req, "pending_approval", RC_APPROVAL_REQUIRED)
    session.add(action)
    session.flush()
    approval = Approval(
        action_id=action.id,
        status="pending",
        expires_at=func.now() + timedelta(minutes=policy.approval_ttl_minutes),
    )
    session.add(approval)
    session.flush()
    audit.record(session, decision="pending_approval", reason_code=RC_APPROVAL_REQUIRED,
                 agent_id=agent.id, action_id=action.id, amount=req.amount,
                 detail={"approval_id": approval.id,
                         "approval_threshold": str(policy.approval_threshold),
                         "expires_in_minutes": policy.approval_ttl_minutes})
    _emit(session, event_type="approval.requested", aggregate_type="approval",
          aggregate_id=approval.id,
          payload={"action_id": action.id, "amount": str(req.amount)})
    logger.info("PENDING_APPROVAL agent=%s amount=%s approval=%s",
                agent.id, req.amount, approval.id)
    return Decision(outcome="pending_approval", reason_code=RC_APPROVAL_REQUIRED,
                    action_id=action.id, approval_id=approval.id,
                    rolling_24h_used=consumed_before + req.amount,
                    rolling_24h_cap=policy.rolling_24h_cap)
