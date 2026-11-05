"""Policy engine tests: caps, allowlist, dedup, approval routing.

Covers the four required scenarios at engine level:
- over-cap refunds (per-transaction AND rolling 24h)
- duplicate requests (idempotency)
- rejected requests (allowlist)
- plus the happy paths and cap-reservation semantics of pending approvals.
"""

from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from gateway.db import session_scope
from gateway.models import Action, Approval, AuditLog
from gateway.policy_engine import (
    RC_ACTION_NOT_ALLOWED,
    RC_APPROVAL_REQUIRED,
    RC_DUPLICATE_REQUEST,
    RC_OK,
    RC_PER_TXN_CAP_EXCEEDED,
    RC_ROLLING_24H_CAP_EXCEEDED,
    ActionRequest,
    rolling_consumption,
)


def req(amount: str, action_type: str = "refund", key: str | None = None) -> ActionRequest:
    return ActionRequest(
        action_type=action_type,
        amount=Decimal(amount),
        currency="USD",
        idempotency_key=key or uuid4().hex,
    )


def audit_rows(agent_id: int) -> list[AuditLog]:
    with session_scope() as s:
        return list(
            s.execute(
                select(AuditLog).where(AuditLog.agent_id == agent_id).order_by(AuditLog.id)
            ).scalars()
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_allowed_within_limits(make_agent, decide):
    agent_id, _ = make_agent()
    d = decide(agent_id, req("25.00"))
    assert d.outcome == "allowed"
    assert d.reason_code == RC_OK
    assert d.action_id > 0
    assert d.rolling_24h_used == Decimal("25.00")
    # Every decision is audited.
    rows = audit_rows(agent_id)
    assert [(r.decision, r.reason_code) for r in rows] == [("allowed", RC_OK)]
    assert rows[0].amount == Decimal("25.00")


def test_pending_approval_at_threshold(make_agent, decide):
    # Threshold 50: amount >= 50 (but <= per_txn 100) routes to approval.
    agent_id, _ = make_agent(threshold="50.00")
    d = decide(agent_id, req("50.00"))
    assert d.outcome == "pending_approval"
    assert d.reason_code == RC_APPROVAL_REQUIRED
    assert d.approval_id is not None
    with session_scope() as s:
        approval = s.get(Approval, d.approval_id)
        assert approval.status == "pending"
        assert approval.action_id == d.action_id


# ---------------------------------------------------------------------------
# Over-cap refunds
# ---------------------------------------------------------------------------

def test_refund_over_per_txn_cap_denied(make_agent, decide):
    # threshold <= per_txn is enforced by ck_policies_threshold_within_cap.
    agent_id, _ = make_agent(per_txn="25.00", threshold="20.00")
    d = decide(agent_id, req("25.01"))
    assert d.outcome == "denied"
    assert d.reason_code == RC_PER_TXN_CAP_EXCEEDED
    assert ("denied", RC_PER_TXN_CAP_EXCEEDED) in [
        (r.decision, r.reason_code) for r in audit_rows(agent_id)
    ]


def test_refund_over_rolling_cap_denied(make_agent, decide):
    # rolling=100: first 60 allowed, second 60 would make 120 > 100.
    # High per_txn/threshold keep every request on the allowed path.
    agent_id, _ = make_agent(per_txn="1000.00", rolling="100.00", threshold="1000.00")
    d1 = decide(agent_id, req("60.00"))
    d2 = decide(agent_id, req("60.00"))
    assert d1.outcome == "allowed"
    assert d2.outcome == "denied"
    assert d2.reason_code == RC_ROLLING_24H_CAP_EXCEEDED
    # A smaller refund that fits the remaining headroom (40) still passes.
    d3 = decide(agent_id, req("40.00"))
    assert d3.outcome == "allowed"
    assert d3.rolling_24h_used == Decimal("100.00")


def test_denied_actions_do_not_consume_cap(make_agent, decide):
    agent_id, _ = make_agent(per_txn="10.00", rolling="100.00", threshold="10.00")
    decide(agent_id, req("999.00"))  # denied: per-txn cap
    with session_scope() as s:
        assert rolling_consumption(s, agent_id) == Decimal("0")


# ---------------------------------------------------------------------------
# Rejected requests (allowlist)
# ---------------------------------------------------------------------------

def test_action_not_in_allowlist_rejected(make_agent, decide):
    agent_id, _ = make_agent(allowed=("refund",))
    d = decide(agent_id, req("10.00", action_type="payout"))
    assert d.outcome == "denied"
    assert d.reason_code == RC_ACTION_NOT_ALLOWED


def test_inactive_agent_denied(make_agent, decide):
    agent_id, _ = make_agent(active=False)
    d = decide(agent_id, req("10.00"))
    assert d.outcome == "denied"
    assert d.reason_code == "AGENT_INACTIVE"
    # No action row is created for a suspended agent; the audit trail has it.
    with session_scope() as s:
        assert s.execute(select(Action)).scalars().all() == []
    assert [(r.decision, r.reason_code) for r in audit_rows(agent_id)] == [
        ("denied", "AGENT_INACTIVE")
    ]


# ---------------------------------------------------------------------------
# Duplicate requests (idempotency)
# ---------------------------------------------------------------------------

def test_duplicate_request_is_deduplicated(make_agent, decide):
    agent_id, _ = make_agent()
    key = uuid4().hex
    d1 = decide(agent_id, req("10.00", key=key))
    d2 = decide(agent_id, req("10.00", key=key))
    assert d1.outcome == "allowed"
    assert d2.outcome == "deduplicated"
    assert d2.reason_code == RC_DUPLICATE_REQUEST
    assert d2.duplicate_of_action_id == d1.action_id
    assert d2.original_outcome == "allowed"
    # Money is counted ONCE: the replay consumed nothing.
    with session_scope() as s:
        assert rolling_consumption(s, agent_id) == Decimal("10.00")


def test_same_key_different_agents_not_deduplicated(make_agent, decide):
    a1, _ = make_agent()
    a2, _ = make_agent()
    key = uuid4().hex
    d1 = decide(a1, req("10.00", key=key))
    d2 = decide(a2, req("10.00", key=key))
    assert d1.outcome == "allowed"
    assert d2.outcome == "allowed"  # dedup scope is per agent


# ---------------------------------------------------------------------------
# Cap reservation semantics for pending approvals
# ---------------------------------------------------------------------------

def test_pending_approval_reserves_cap_until_rejected(make_agent, decide):
    # rolling=100, threshold=40: a pending 60 reserves headroom...
    agent_id, _ = make_agent(per_txn="60.00", rolling="100.00", threshold="40.00")
    d1 = decide(agent_id, req("60.00"))
    assert d1.outcome == "pending_approval"
    d2 = decide(agent_id, req("50.00"))
    assert d2.outcome == "denied"  # 60 reserved + 50 > 100
    assert d2.reason_code == RC_ROLLING_24H_CAP_EXCEEDED

    # ...but rejecting the approval RELEASES the reservation.
    from gateway.approvals import decide as decide_approval

    with session_scope() as s:
        decide_approval(s, d1.approval_id, "rejected", "ops-test")
    d3 = decide(agent_id, req("50.00"))
    assert d3.outcome == "pending_approval"  # 50 >= threshold, cap free again
