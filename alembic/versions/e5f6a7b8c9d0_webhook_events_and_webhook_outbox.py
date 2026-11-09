"""webhook_events + webhook_outbox

Two new tables for the PayPal webhook ingestion pipeline:

- webhook_events: one row per accepted webhook delivery. The UNIQUE
  constraint on external_event_id makes duplicate deliveries idempotent
  at the database level.
- webhook_outbox: the job queue the background worker consumes. Separate
  from the existing domain-event 'outbox' table on purpose — that one is
  a publish-notification log (pending/published/failed, no retry
  machinery); this one is a claimable job queue (pending/processing/
  processed/dead) with attempts, backoff scheduling, lease timestamps,
  and dead-letter columns.

Indexes:
- uq_webhook_outbox_dedup: partial unique (job_type, dedup_key) — the
  second duplicate guard (two different event ids referencing the SAME
  dispute enqueue only one agent job). NULL dedup_key = no dedup.
- ix_webhook_outbox_pending: claim query (status='pending' AND
  next_attempt_at <= now) stays index-only over the hot subset.
- ix_webhook_outbox_processing: lease-recovery sweep (stale claimed_at).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-11-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('webhook_events',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('external_event_id', sa.Text(), nullable=False),
    sa.Column('event_type', sa.Text(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('verification', sa.Text(), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("verification IN ('paypal', 'shared_secret')", name='ck_webhook_events_verification_valid'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('external_event_id', name='uq_webhook_events_external_id'),
    )
    op.create_table('webhook_outbox',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('job_type', sa.Text(), nullable=False),
    sa.Column('dedup_key', sa.Text(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('status', sa.Text(), server_default='pending', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("job_type IN ('dispute_agent', 'resume_agent')", name='ck_webhook_outbox_job_type_valid'),
    sa.CheckConstraint("status IN ('pending', 'processing', 'processed', 'dead')", name='ck_webhook_outbox_status_valid'),
    sa.CheckConstraint('attempts >= 0', name='ck_webhook_outbox_attempts_nonneg'),
    sa.CheckConstraint('failed_at IS NULL OR failed_at >= created_at', name='ck_webhook_outbox_failed_after_created'),
    sa.CheckConstraint('processed_at IS NULL OR processed_at >= created_at', name='ck_webhook_outbox_processed_after_created'),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_webhook_outbox_dedup', 'webhook_outbox', ['job_type', 'dedup_key'], unique=True, postgresql_where=sa.text('dedup_key IS NOT NULL'))
    op.create_index('ix_webhook_outbox_pending', 'webhook_outbox', ['next_attempt_at'], unique=False, postgresql_where=sa.text("status = 'pending'"))
    op.create_index('ix_webhook_outbox_processing', 'webhook_outbox', ['claimed_at'], unique=False, postgresql_where=sa.text("status = 'processing'"))


def downgrade() -> None:
    op.drop_index('ix_webhook_outbox_processing', table_name='webhook_outbox')
    op.drop_index('ix_webhook_outbox_pending', table_name='webhook_outbox')
    op.drop_index('uq_webhook_outbox_dedup', table_name='webhook_outbox')
    op.drop_table('webhook_outbox')
    op.drop_table('webhook_events')
