"""agent_keys rotation table; extend audit decisions for admin ops

Revision ID: 7f9c7b6f0fe1
Revises: fa956bf87c5a
Create Date: 2026-11-05 10:47:24.572685

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7f9c7b6f0fe1'
down_revision: Union[str, Sequence[str], None] = 'fa956bf87c5a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. New rotation table.
    op.create_table('agent_keys',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('agent_id', sa.BigInteger(), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('key_prefix', sa.String(length=16), nullable=False),
    sa.Column('label', sa.Text(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('revoked_at IS NULL OR revoked_at >= created_at', name='ck_agent_keys_revoked_after_created'),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_hash')
    )
    op.create_index(op.f('ix_agent_keys_agent_id'), 'agent_keys', ['agent_id'], unique=False)

    # 2. Carry every existing agent's credential over BEFORE the columns die.
    #    (Autogenerate never writes data migrations — this step is manual on
    #    purpose. Skipping it would lock every live agent out.)
    op.execute(
        "INSERT INTO agent_keys (agent_id, key_hash, key_prefix, label) "
        "SELECT id, api_key_hash, api_key_prefix, 'primary' FROM agents"
    )

    # 3. Now safe to drop the old one-key-per-agent columns.
    op.drop_constraint(op.f('agents_api_key_hash_key'), 'agents', type_='unique')
    op.drop_column('agents', 'api_key_hash')
    op.drop_column('agents', 'api_key_prefix')

    # 4. Widen the audit decision vocabulary for admin provisioning ops.
    #    (Autogenerate does not detect changed CHECK expressions — manual.)
    op.drop_constraint('ck_audit_decision_valid', 'audit_log', type_='check')
    op.create_check_constraint(
        'ck_audit_decision_valid', 'audit_log',
        "decision IN ('pending', 'approved', 'rejected', 'expired', 'executed', "
        "'failed', 'allowed', 'denied', 'pending_approval', 'deduplicated', "
        "'auth_failed', 'rate_limited', 'agent_created', 'key_issued', 'key_revoked')",
    )


def downgrade() -> None:
    """Downgrade schema (best effort: keeps each agent's OLDEST live key)."""
    # 1. Restore columns as nullable, then backfill from agent_keys.
    op.add_column('agents', sa.Column('api_key_prefix', sa.VARCHAR(length=16), nullable=True))
    op.add_column('agents', sa.Column('api_key_hash', sa.VARCHAR(length=64), nullable=True))
    op.execute(
        "UPDATE agents a SET api_key_hash = k.key_hash, api_key_prefix = k.key_prefix "
        "FROM (SELECT DISTINCT ON (agent_id) agent_id, key_hash, key_prefix "
        "      FROM agent_keys WHERE revoked_at IS NULL "
        "      ORDER BY agent_id, created_at) k "
        "WHERE a.id = k.agent_id"
    )
    # Agents whose keys were all revoked cannot satisfy the old NOT NULL
    # one-key schema; they must be deleted (their credentials are gone anyway).
    op.execute("DELETE FROM agents WHERE api_key_hash IS NULL")
    op.alter_column('agents', 'api_key_hash', nullable=False)
    op.alter_column('agents', 'api_key_prefix', nullable=False)
    op.create_unique_constraint(op.f('agents_api_key_hash_key'), 'agents', ['api_key_hash'], postgresql_nulls_not_distinct=False)

    # 2. Restore the narrower audit vocabulary (rows using admin decisions
    #    would violate it — rewrite them to 'auth_failed' is wrong, so this
    #    downgrade refuses if such rows exist; delete them consciously first).
    op.drop_constraint('ck_audit_decision_valid', 'audit_log', type_='check')
    op.create_check_constraint(
        'ck_audit_decision_valid', 'audit_log',
        "decision IN ('pending', 'approved', 'rejected', 'expired', 'executed', "
        "'failed', 'allowed', 'denied', 'pending_approval', 'deduplicated', "
        "'auth_failed', 'rate_limited')",
    )

    op.drop_index(op.f('ix_agent_keys_agent_id'), table_name='agent_keys')
    op.drop_table('agent_keys')
