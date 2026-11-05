"""Approval lifecycle tests: transitions, illegal moves, expiration."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from gateway import approvals as approvals_mod
from gateway.db import session_scope
from gateway.models import Approval
from gateway.policy_engine import ActionRequest, rolling_consumption


def _pending_agent(make_agent, decide, amount: str = "60.00"):
    """Agent whose policy forces pending_approval; returns (agent_id, decision)."""
    agent_id, _ = make_agent(per_txn="100.00", rolling="500.00", threshold="50.00")
    d = decide(
        agent_id,
        ActionRequest(
            action_type="refund", amount=Decimal(amount),
            currency="USD", idempotency_key=uuid4().hex,
        ),
    )
    assert d.outcome == "pending_approval"
    return agent_id, d


def _backdate_expiry(approval_id: int) -> None:
    """Make an approval stale without violating ck_approvals_expiry_future:
    shift requested_at and expires_at together into the past."""
    with session_scope() as s:
        s.execute(
            text(
                "UPDATE approvals SET requested_at = now() - interval '2 hours',"
                " expires_at = now() - interval '1 hour' WHERE id = :aid"
            ),
            {"aid": approval_id},
        )


def test_full_lifecycle_approved_executed(make_agent, decide):
    _, d = _pending_agent(make_agent, decide)
    with session_scope() as s:
        a = approvals_mod.decide(s, d.approval_id, "approved", "ops-alice")
        assert a.status == "approved"
        assert a.decided_by == "ops-alice"
        assert a.decided_at is not None
    with session_scope() as s:
        a = approvals_mod.mark_execution(s, d.approval_id, executed=True)
        assert a.status == "executed"


def test_rejected_is_terminal(make_agent, decide):
    _, d = _pending_agent(make_agent, decide)
    with session_scope() as s:
        a = approvals_mod.decide(s, d.approval_id, "rejected", "ops-bob")
        assert a.status == "rejected"
    with session_scope() as s:
        with pytest.raises(approvals_mod.InvalidTransitionError):
            approvals_mod.decide(s, d.approval_id, "approved", "ops-eve")


def test_illegal_transitions_raise(make_agent, decide):
    _, d = _pending_agent(make_agent, decide)
    with session_scope() as s:
        # Cannot execute before approving.
        with pytest.raises(approvals_mod.InvalidTransitionError) as exc_info:
            approvals_mod.mark_execution(s, d.approval_id, executed=True)
        assert "pending" in str(exc_info.value)


def test_execution_failure_recorded(make_agent, decide):
    _, d = _pending_agent(make_agent, decide)
    with session_scope() as s:
        approvals_mod.decide(s, d.approval_id, "approved", "ops-alice")
    with session_scope() as s:
        a = approvals_mod.mark_execution(
            s, d.approval_id, executed=False, reason_code="PAYPAL_500"
        )
        assert a.status == "failed"
        assert a.reason_code == "PAYPAL_500"


def test_expiry_sweep(make_agent, decide):
    agent_id, d = _pending_agent(make_agent, decide)
    _backdate_expiry(d.approval_id)

    with session_scope() as s:
        expired = approvals_mod.expire_stale_approvals(s)
    assert expired == [d.approval_id]
    with session_scope() as s:
        a = s.get(Approval, d.approval_id)
        assert a.status == "expired"
        assert a.reason_code == approvals_mod.RC_EXPIRED

    # Expiring released the cap reservation: the same money fits again.
    with session_scope() as s:
        assert rolling_consumption(s, agent_id) == Decimal("0")

    # Second sweep is a no-op (idempotent).
    with session_scope() as s:
        assert approvals_mod.expire_stale_approvals(s) == []


def test_sweep_skips_fresh_approvals(make_agent, decide):
    _, d = _pending_agent(make_agent, decide)  # expires in 60 min
    with session_scope() as s:
        assert approvals_mod.expire_stale_approvals(s) == []
    with session_scope() as s:
        assert s.get(Approval, d.approval_id).status == "pending"
