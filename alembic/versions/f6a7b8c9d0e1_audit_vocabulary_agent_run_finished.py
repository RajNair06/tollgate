"""audit vocabulary: agent_run_finished

Widens ck_audit_decision_valid with 'agent_run_finished' — the row the
outbox worker writes after each dispatched dispute-agent run completes
(or pauses at an approval interrupt), so the FINAL OUTCOME of every
webhook-triggered run is discoverable in the gateway's audit_log, next to
the money-decision rows. reason_code carries the run's terminal status
(refund_executed, evidence_failed, pending_approval, ...); detail carries
run_id, dispute_id, summary, and token usage.

Same drop/recreate pattern as migrations 0003 and d4e5f6a7b8c9 —
autogenerate does not detect changed CHECK expressions.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-09
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALL_DECISIONS = (
    "decision IN ('pending', 'approved', 'rejected', 'expired', 'executed', "
    "'failed', 'allowed', 'denied', 'pending_approval', 'deduplicated', "
    "'auth_failed', 'rate_limited', 'agent_created', 'key_issued', "
    "'key_revoked', 'agent_updated', 'agent_run_finished')"
)
_PREV_DECISIONS = (
    "decision IN ('pending', 'approved', 'rejected', 'expired', 'executed', "
    "'failed', 'allowed', 'denied', 'pending_approval', 'deduplicated', "
    "'auth_failed', 'rate_limited', 'agent_created', 'key_issued', "
    "'key_revoked', 'agent_updated')"
)


def upgrade() -> None:
    op.drop_constraint('ck_audit_decision_valid', 'audit_log', type_='check')
    op.create_check_constraint(
        'ck_audit_decision_valid', 'audit_log', _ALL_DECISIONS)


def downgrade() -> None:
    # Best effort: fails if 'agent_run_finished' rows already exist.
    op.drop_constraint('ck_audit_decision_valid', 'audit_log', type_='check')
    op.create_check_constraint(
        'ck_audit_decision_valid', 'audit_log', _PREV_DECISIONS)
