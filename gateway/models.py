"""ORM models: the authorization database schema.

Tables
------
agents      who may call the gateway (API key hash = identity)
policies    per-agent rules: allowlist, caps, approval threshold (1:1 agents)
actions     every authorization request and its outcome (idempotency lives here)
approvals   human-in-the-loop workflow for pending_approval actions
audit_log   append-only record of every decision (no UPDATE/DELETE anywhere)
events      immutable domain-event log
outbox      transactional outbox: events written in the SAME transaction as
            the decision, published later by a relay (no lost notifications)
webhook_events   accepted PayPal webhook deliveries (UNIQUE external event id
            makes duplicate deliveries idempotent at the database level)
webhook_outbox   job queue for the background worker: claimable rows with
            attempts, backoff scheduling, lease timestamps, dead-lettering.
            Separate from 'outbox' on purpose — that is a publish log
            (pending/published/failed); this is a job queue
            (pending/processing/processed/dead).

Design choices
--------------
- TEXT + CHECK for statuses instead of native ENUM: adding a state is a
  constraint migration, not an ALTER TYPE ... which locks less and travels
  through Alembic cleanly.
- NUMERIC(12,2) for money: exact decimal, matches PayPal string amounts.
- audit_log.agent_id is nullable so pre-auth failures (bad API key) are
  auditable too. FK has NO cascade: audit rows must block agent deletion.
- The perf-sensitive index on audit_log(agent_id, created_at) is added in a
  SEPARATE later migration — deliberate, for the EXPLAIN ANALYZE exercise.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Canonical vocabularies — single source of truth, reused by the CHECKs below
# and by application code (policy_engine, approvals).
ACTION_OUTCOMES = ("allowed", "denied", "pending_approval", "deduplicated")
APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired", "executed", "failed")
AUDIT_DECISIONS = APPROVAL_STATUSES + ACTION_OUTCOMES + (
    "auth_failed",
    "rate_limited",
    # Admin provisioning operations (migration 0003 widened the CHECK for these).
    "agent_created",
    "key_issued",
    "key_revoked",
    "agent_updated",
)

# Webhook pipeline vocabularies (migration e5f6a7b8c9d0).
WEBHOOK_VERIFICATIONS = ("paypal", "shared_secret")
WEBHOOK_JOB_TYPES = ("dispute_agent", "resume_agent")
WEBHOOK_OUTBOX_STATUSES = ("pending", "processing", "processed", "dead")

# Legal approval status transitions (application-enforced in approvals.py).
APPROVAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("approved", "rejected", "expired"),
    "approved": ("executed", "failed", "expired"),
    "rejected": (),
    "expired": (),
    "executed": (),
    "failed": (),
}


class Base(DeclarativeBase):
    pass


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    policy: Mapped["Policy"] = relationship(back_populates="agent", uselist=False)
    actions: Mapped[list["Action"]] = relationship(back_populates="agent")
    keys: Mapped[list["AgentKey"]] = relationship(back_populates="agent")


class AgentKey(Base):
    """One API key for one agent. 1:N so keys can ROTATE without downtime:
    issue a new key, let the agent migrate to it, then revoke the old one.

    Only the SHA-256 hash is stored (64 hex chars). The plaintext key exists
    exactly once — in the provisioning response — and can never be retrieved
    again, by anyone, including admins.

    Revocation is soft (revoked_at timestamp): the row stays for forensics,
    and auth rejects it from the next request onward. No process restart,
    no cache invalidation — the database is the only source of truth.
    """

    __tablename__ = "agent_keys"
    __table_args__ = (
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_agent_keys_revoked_after_created",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # First 11 chars ("tg_" + 8) — not secret; lets ops identify a key in logs.
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # Optional human label, e.g. "primary" or "rotation-2026-11".
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    agent: Mapped[Agent] = relationship(back_populates="keys")


class Policy(Base):
    __tablename__ = "policies"
    __table_args__ = (
        CheckConstraint("per_txn_cap > 0", name="ck_policies_per_txn_cap_positive"),
        CheckConstraint("rolling_24h_cap > 0", name="ck_policies_rolling_cap_positive"),
        CheckConstraint("approval_threshold > 0", name="ck_policies_threshold_positive"),
        # Coherence: if threshold exceeded the per-txn cap, the approval path
        # would be unreachable (cap denies first) — almost certainly a config bug.
        CheckConstraint(
            "approval_threshold <= per_txn_cap", name="ck_policies_threshold_within_cap"
        ),
        CheckConstraint("approval_ttl_minutes > 0", name="ck_policies_ttl_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # exactly one policy per agent
    )
    allowed_actions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    per_txn_cap: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    rolling_24h_cap: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    approval_threshold: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    approval_ttl_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="60"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    agent: Mapped[Agent] = relationship(back_populates="policy")


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (
        # Partial unique index: a live (non-deduplicated) request may use an
        # idempotency key only ONCE per agent. Replays are logged as separate
        # rows with outcome='deduplicated' and are exempt from uniqueness.
        # Hard backstop for the in-transaction dedup check.
        Index(
            "uq_actions_agent_idem",
            "agent_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("outcome <> 'deduplicated'"),
        ),
        CheckConstraint("amount > 0", name="ck_actions_amount_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_actions_currency_iso"),
        CheckConstraint(
            f"outcome IN {ACTION_OUTCOMES}", name="ck_actions_outcome_valid"
        ),
        CheckConstraint("reason_code IS NOT NULL", name="ck_actions_reason_present"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agents.id"), nullable=False, index=True
    )
    action_type: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. "refund"
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD")
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    # External resource this action targets (e.g. PayPal capture ID).
    reference_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when outcome='deduplicated': the original action this is a replay of.
    duplicate_of_action_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("actions.id"), nullable=True
    )
    # Set when an 'allowed' action's PayPal execution failed DEFINITELY (4xx —
    # provably not executed). Releases the amount from the 24h rolling cap.
    # The decision row itself is immutable: outcome stays 'allowed' because
    # the decision WAS allowed; this column records what happened next.
    # Ambiguous failures (timeout, 5xx) leave it NULL — the money may have
    # moved, so the reservation is kept (conservative).
    execution_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    agent: Mapped[Agent] = relationship(back_populates="actions")
    approval: Mapped["Approval | None"] = relationship(back_populates="action", uselist=False)


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            f"status IN {APPROVAL_STATUSES}", name="ck_approvals_status_valid"
        ),
        CheckConstraint("expires_at > requested_at", name="ck_approvals_expiry_future"),
        CheckConstraint(
            "decided_at IS NULL OR decided_at >= requested_at",
            name="ck_approvals_decided_after_request",
        ),
        CheckConstraint(
            "(status = 'pending' AND decided_at IS NULL)"
            " OR (status <> 'pending' AND decided_at IS NOT NULL)",
            name="ck_approvals_decided_at_consistency",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    action_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("actions.id"), nullable=False, unique=True  # 1:1
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    action: Mapped[Action] = relationship(back_populates="approval")


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(f"decision IN {AUDIT_DECISIONS}", name="ck_audit_decision_valid"),
        CheckConstraint(
            "amount IS NULL OR amount > 0", name="ck_audit_amount_positive_or_null"
        ),
        # Perf index (migration 0002, added AFTER recording the seq-scan
        # baseline — before/after plans summarized in README §Performance).
        # Composite (agent_id, created_at DESC) answers "one agent's recent history"
        # directly; INCLUDE columns make the typical aggregate query
        # index-only (no heap fetches at all).
        Index(
            "ix_audit_log_agent_created",
            "agent_id",
            text("created_at DESC"),
            postgresql_include=["decision", "reason_code", "amount"],
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Nullable: pre-authentication failures have no agent yet.
    agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id"), nullable=True
    )
    action_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("actions.id"), nullable=True
    )
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Deliberately NO relationships with cascade, NO update helpers anywhere:
    # append-only is enforced by the application (audit.py exposes insert only).


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "aggregate_type IN ('agent', 'action', 'approval')",
            name="ck_events_aggregate_type_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)  # action.authorized ...
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'published', 'failed')", name="ck_outbox_status_valid"
        ),
        CheckConstraint(
            "published_at IS NULL OR published_at >= created_at",
            name="ck_outbox_published_after_created",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookEvent(Base):
    """One accepted PayPal webhook delivery.

    The UNIQUE constraint on external_event_id is the FIRST duplicate guard:
    PayPal retries deliveries (and may deliver the same event more than once),
    so the event id — not our processing — decides uniqueness. A duplicate
    insert is caught, and the endpoint returns 200 without enqueueing again.

    'verification' records WHICH strategy authenticated the delivery
    ('paypal' = transmission-signature verification via the PayPal API,
    'shared_secret' = local HMAC used by the dev simulator) — forensics
    answer "how did this get in?" without re-deriving it.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        CheckConstraint(
            f"verification IN {WEBHOOK_VERIFICATIONS}",
            name="ck_webhook_events_verification_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    external_event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)  # CHECKOUT.DISPUTE.CREATED ...
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    verification: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WebhookOutbox(Base):
    """Job queue consumed by the background worker (worker/ package).

    Lifecycle: pending → processing (claimed via SELECT FOR UPDATE SKIP
    LOCKED, claimed_at = lease start) → processed, or back to pending with
    attempts++ and next_attempt_at pushed out exponentially, or → dead
    after max attempts (last_error + failed_at preserved for the dashboard's
    dead-letter view). Crash recovery: rows stuck in 'processing' past the
    lease timeout are reclaimed — the worker never trusts claimed_at alone.

    dedup_key is the SECOND duplicate guard: two DIFFERENT event ids that
    reference the same dispute (e.g. created + updated) produce the same
    key ("dispute:{id}"), and the partial unique index on
    (job_type, dedup_key) makes the second enqueue a no-op. NULL = no dedup.
    """

    __tablename__ = "webhook_outbox"
    __table_args__ = (
        CheckConstraint(
            f"job_type IN {WEBHOOK_JOB_TYPES}", name="ck_webhook_outbox_job_type_valid"
        ),
        CheckConstraint(
            f"status IN {WEBHOOK_OUTBOX_STATUSES}", name="ck_webhook_outbox_status_valid"
        ),
        CheckConstraint("attempts >= 0", name="ck_webhook_outbox_attempts_nonneg"),
        CheckConstraint(
            "failed_at IS NULL OR failed_at >= created_at",
            name="ck_webhook_outbox_failed_after_created",
        ),
        CheckConstraint(
            "processed_at IS NULL OR processed_at >= created_at",
            name="ck_webhook_outbox_processed_after_created",
        ),
        # Partial UNIQUE: only rows WITH a dedup_key participate.
        Index(
            "uq_webhook_outbox_dedup", "job_type", "dedup_key",
            unique=True, postgresql_where=text("dedup_key IS NOT NULL"),
        ),
        # Claim query: WHERE status='pending' AND next_attempt_at <= now.
        Index(
            "ix_webhook_outbox_pending", "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # Lease-recovery sweep over stale 'processing' rows.
        Index(
            "ix_webhook_outbox_processing", "claimed_at",
            postgresql_where=text("status = 'processing'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    dedup_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
