"""audit vocabulary: agent_updated

Widens ck_audit_decision_valid with 'agent_updated' for the new
PATCH /v1/admin/agents/{id} endpoint (kill switch + policy updates).
Same drop/recreate pattern as migration 0003 — autogenerate does not
detect changed CHECK expressions.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-11-07
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALL_DECISIONS = (
    "decision IN ('pending', 'approved', 'rejected', 'expired', 'executed', "
    "'failed', 'allowed', 'denied', 'pending_approval', 'deduplicated', "
    "'auth_failed', 'rate_limited', 'agent_created', 'key_issued', "
    "'key_revoked', 'agent_updated')"
)
_PREV_DECISIONS = (
    "decision IN ('pending', 'approved', 'rejected', 'expired', 'executed', "
    "'failed', 'allowed', 'denied', 'pending_approval', 'deduplicated', "
    "'auth_failed', 'rate_limited', 'agent_created', 'key_issued', 'key_revoked')"
)


def upgrade() -> None:
    op.drop_constraint('ck_audit_decision_valid', 'audit_log', type_='check')
    op.create_check_constraint(
        'ck_audit_decision_valid', 'audit_log', _ALL_DECISIONS)


def downgrade() -> None:
    # Best effort: fails if 'agent_updated' rows already exist (delete or
    # rewrite them first) — the CHECK cannot shrink under live data.
    op.drop_constraint('ck_audit_decision_valid', 'audit_log', type_='check')
    op.create_check_constraint(
        'ck_audit_decision_valid', 'audit_log', _PREV_DECISIONS)
