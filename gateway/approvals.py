"""Approval lifecycle: state machine + expiration.

States:  pending → approved → executed
                ↘ rejected      ↘ failed
         pending/approved → expired (TTL passed)

Legal transitions live in models.APPROVAL_TRANSITIONS — one source of truth,
enforced here under a row lock (SELECT ... FOR UPDATE) so two concurrent
decisions on the same approval cannot both succeed.

Every transition writes: the approvals row, an audit_log row (decision =
new status, with reason code), an event, and an outbox row — all in the
caller's transaction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gateway import audit
from gateway.models import APPROVAL_TRANSITIONS, Approval, Event, Outbox

logger = logging.getLogger(__name__)

RC_APPROVED = "APPROVED_BY_OPERATOR"
RC_REJECTED = "REJECTED_BY_OPERATOR"
RC_EXPIRED = "EXPIRED"
RC_EXECUTED = "EXECUTED"
RC_EXECUTION_FAILED = "EXECUTION_FAILED"


class InvalidTransitionError(Exception):
    """Requested status change violates the state machine."""

    def __init__(self, approval_id: int, current: str, requested: str):
        self.approval_id = approval_id
        self.current = current
        self.requested = requested
        super().__init__(
            f"approval {approval_id}: {current} → {requested} is not a legal transition "
            f"(allowed from {current!r}: {list(APPROVAL_TRANSITIONS[current]) or 'none — terminal'})"
        )


class ApprovalNotFoundError(Exception):
    pass


def _load_for_update(session: Session, approval_id: int) -> Approval:
    approval = session.execute(
        select(Approval).where(Approval.id == approval_id).with_for_update()
    ).scalar_one_or_none()
    if approval is None:
        raise ApprovalNotFoundError(f"approval {approval_id} not found")
    return approval


def _transition(session: Session, approval: Approval, new_status: str,
                reason_code: str, decided_by: str | None) -> None:
    """Validate + apply a transition, then write audit/event/outbox rows."""
    allowed = APPROVAL_TRANSITIONS[approval.status]
    if new_status not in allowed:
        raise InvalidTransitionError(approval.id, approval.status, new_status)

    old_status = approval.status
    approval.status = new_status
    approval.reason_code = reason_code
    approval.decided_at = datetime.now(timezone.utc)
    if decided_by is not None:
        approval.decided_by = decided_by
    session.flush()

    audit.record(
        session, decision=new_status, reason_code=reason_code,
        agent_id=approval.action.agent_id if approval.action else None,
        action_id=approval.action_id,
        amount=approval.action.amount if approval.action else None,
        detail={"approval_id": approval.id, "from": old_status, "to": new_status,
                "decided_by": decided_by},
    )
    payload = {"approval_id": approval.id, "action_id": approval.action_id,
               "from": old_status, "to": new_status, "reason_code": reason_code}
    session.add(Event(event_type=f"approval.{new_status}", aggregate_type="approval",
                      aggregate_id=approval.id, payload=payload))
    session.add(Outbox(event_type=f"approval.{new_status}", aggregate_type="approval",
                       aggregate_id=approval.id, payload=payload))
    logger.info("approval %s: %s → %s (%s)", approval.id, old_status, new_status, reason_code)


def decide(session: Session, approval_id: int, decision: str,
           decided_by: str) -> Approval:
    """Human decision: pending → approved | rejected."""
    approval = _load_for_update(session, approval_id)
    if decision == "approved":
        _transition(session, approval, "approved", RC_APPROVED, decided_by)
    elif decision == "rejected":
        _transition(session, approval, "rejected", RC_REJECTED, decided_by)
    else:
        raise ValueError(f"decision must be 'approved' or 'rejected', got {decision!r}")
    return approval


def mark_execution(session: Session, approval_id: int, executed: bool,
                   reason_code: str | None = None) -> Approval:
    """Post-execution bookkeeping: approved → executed | failed.

    Called after the gateway performed (or attempted) the PayPal refund.
    """
    approval = _load_for_update(session, approval_id)
    if executed:
        _transition(session, approval, "executed", reason_code or RC_EXECUTED, None)
    else:
        _transition(session, approval, "failed", reason_code or RC_EXECUTION_FAILED, None)
    return approval


def expire_stale_approvals(session: Session) -> list[int]:
    """Sweep: pending approvals past their expires_at → expired.

    Locks the matching rows (FOR UPDATE SKIP LOCKED) so concurrent sweeps
    and human decisions never collide: a decision in flight keeps its row,
    the sweep simply skips it and the next pass re-checks.

    Expiring releases the rolling-cap reservation (the engine only counts
    pending/approved/executed), so stale requests stop blocking new ones.

    Normally run by a scheduler; also exposed as an endpoint for ops.
    Returns the IDs of approvals expired by this call.
    """
    stale = session.execute(
        select(Approval)
        .where(Approval.status == "pending", Approval.expires_at < func.now())
        .with_for_update(skip_locked=True)
    ).scalars().all()

    expired_ids = []
    for approval in stale:
        _transition(session, approval, "expired", RC_EXPIRED, None)
        expired_ids.append(approval.id)
    if expired_ids:
        logger.info("Expired %s stale approval(s): %s", len(expired_ids), expired_ids)
    return expired_ids
