"""Job-queue primitives over webhook_outbox.

Every function takes an open Session and does NOT commit — the caller owns
transaction boundaries. That keeps the two phases separate:

- claim phase: one SHORT transaction (SELECT FOR UPDATE SKIP LOCKED +
  status='processing' + commit). Row locks live only as long as the claim.
- work phase: the handler runs OUTSIDE any transaction, protected by the
  lease (status='processing' + claimed_at), not by held locks. Long agent
  runs never pin database locks or bloat a long transaction.

SKIP LOCKED is what makes horizontal scaling safe: N workers poll the same
table and never claim the same row — a locked row is skipped, not waited on.
"""

from __future__ import annotations

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from gateway.models import WebhookOutbox


def _in_seconds(seconds: float):
    """DB-clock interval: now() + make_interval(secs => seconds), built with
    positional args (years, months, weeks, days, hours, mins, secs). Using
    the DB clock (not Python's) keeps scheduling honest across machines."""
    return func.make_interval(0, 0, 0, 0, 0, 0, seconds)


def enqueue_job(
    session: Session,
    job_type: str,
    payload: dict,
    dedup_key: str | None = None,
) -> int | None:
    """Insert a pending job. Returns its id, or None if a row with the same
    (job_type, dedup_key) already exists (partial unique index — dedup only
    applies when dedup_key is not NULL). Caller commits."""
    result = session.execute(
        pg_insert(WebhookOutbox)
        .values(job_type=job_type, payload=payload, dedup_key=dedup_key)
        .on_conflict_do_nothing(
            index_elements=["job_type", "dedup_key"],
            index_where=WebhookOutbox.dedup_key.is_not(None),
        )
        .returning(WebhookOutbox.id)
    )
    return result.scalar_one_or_none()


def claim_batch(session: Session, batch_size: int) -> list[WebhookOutbox]:
    """Claim up to batch_size due jobs: pending AND next_attempt_at <= now,
    oldest first. Selected rows are flipped to 'processing' with a fresh
    claimed_at (the lease) and attempts++. Caller commits to release locks.

    FOR UPDATE SKIP LOCKED: rows another worker is claiming RIGHT NOW are
    invisible to this query instead of blocking it.
    """
    rows = session.execute(
        select(WebhookOutbox)
        .where(WebhookOutbox.status == "pending",
               WebhookOutbox.next_attempt_at <= func.now())
        .order_by(WebhookOutbox.next_attempt_at, WebhookOutbox.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).scalars().all()

    for row in rows:
        row.status = "processing"
        row.claimed_at = func.now()
        row.attempts = row.attempts + 1
    session.flush()
    return list(rows)


def mark_processed(session: Session, job_id: int) -> None:
    session.execute(
        update(WebhookOutbox)
        .where(WebhookOutbox.id == job_id)
        .values(status="processed", processed_at=func.now())
    )


def reschedule(session: Session, job_id: int, delay_seconds: float,
               last_error: str) -> None:
    """Retryable failure: back to pending, retry at now + delay. attempts
    was already incremented at claim time; claimed_at is cleared so the
    lease sweep ignores the row."""
    session.execute(
        update(WebhookOutbox)
        .where(WebhookOutbox.id == job_id)
        .values(status="pending", claimed_at=None, last_error=last_error,
                next_attempt_at=func.now() + _in_seconds(delay_seconds))
    )


def mark_dead(session: Session, job_id: int, last_error: str) -> None:
    """Terminal failure: the dead-letter view (dashboard) reads these."""
    session.execute(
        update(WebhookOutbox)
        .where(WebhookOutbox.id == job_id)
        .values(status="dead", claimed_at=None, last_error=last_error,
                failed_at=func.now())
    )


def recover_stale_leases(session: Session, lease_seconds: float,
                         max_attempts: int) -> int:
    """Crash recovery. A worker that died mid-job leaves its row in
    'processing' forever; the lease (claimed_at) is how we notice. Rows
    stale beyond lease_seconds go back to pending — UNLESS attempts are
    exhausted (a job that keeps crashing workers must not loop forever),
    in which case they dead-letter with the crash as the recorded reason.

    One statement, no row iteration. Returns the number of rows touched.
    Caller commits.
    """
    exhausted_note = ("LEASE_EXPIRED: worker crashed or stalled; attempts "
                      "exhausted during recovery — dead-lettered")
    reclaim_note = "LEASE_EXPIRED: worker crashed or stalled; reclaimed by lease sweep"
    exhausted = WebhookOutbox.attempts >= max_attempts
    result = session.execute(
        update(WebhookOutbox)
        .where(
            WebhookOutbox.status == "processing",
            WebhookOutbox.claimed_at < func.now() - _in_seconds(lease_seconds),
        )
        .values(
            status=case((exhausted, "dead"), else_="pending"),
            failed_at=case((exhausted, func.now()), else_=None),
            last_error=case((exhausted, exhausted_note), else_=reclaim_note),
            claimed_at=None,
        )
    )
    return result.rowcount or 0
