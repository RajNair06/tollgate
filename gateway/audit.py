"""Append-only audit writer.

Contract: this module is the ONLY place that writes audit_log rows, and it
exposes exactly one operation — record(). There is no update function and no
delete function. Append-only is enforced at the application level by the
absence of any other code path (no ORM relationship cascades touch audit_log
either; its FKs deliberately have no ON DELETE).

Every decision — allowed, denied, pending_approval, deduplicated, approval
transitions, auth failures, rate-limit rejections — gets one row, written in
the SAME transaction as the decision itself. If the decision rolls back, its
audit row rolls back with it: the log can never claim something that did not
happen.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from gateway.models import AuditLog


def record(
    session: Session,
    *,
    decision: str,
    reason_code: str,
    agent_id: int | None = None,
    action_id: int | None = None,
    amount: Decimal | None = None,
    detail: dict | None = None,
) -> AuditLog:
    """Append one audit row. Caller's transaction owns commit/rollback."""
    row = AuditLog(
        agent_id=agent_id,
        action_id=action_id,
        decision=decision,
        reason_code=reason_code,
        amount=amount,
        detail=detail or {},
    )
    session.add(row)
    return row
