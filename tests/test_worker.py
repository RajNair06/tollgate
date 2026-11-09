"""Outbox worker tests against real PostgreSQL.

What is being proven:
- SKIP LOCKED claiming: concurrent claimers never get the same row, and
  never block each other.
- Due-time filtering and batch limits.
- The three finalize paths: processed / rescheduled-with-backoff / dead.
- Exponential, jittered, capped backoff.
- Dead-lettering: permanent errors immediately, retryable errors at
  max_attempts, with last_error + failed_at recorded.
- Lease-based crash recovery: stale 'processing' rows are reclaimed,
  fresh ones untouched, exhausted ones dead-lettered.
- Two racing workers process a job exactly once.
"""

import threading
import time
from datetime import datetime

from sqlalchemy import select, text, update

from gateway.db import get_engine, session_scope
from gateway.models import WebhookOutbox
from worker.config import WorkerSettings
from worker.errors import PermanentJobError, RetryableJobError
from worker.queue import claim_batch, enqueue_job, recover_stale_leases
from worker.worker import Worker, backoff_delay

FAST = WorkerSettings(
    database_url="",  # unused: tests inject the conftest engine
    max_attempts=3,
    backoff_base_seconds=0.02,
    backoff_max_seconds=0.1,
    lease_seconds=300.0,
    poll_interval_seconds=0.01,
    batch_size=5,
)


def _fetch(job_id: int) -> WebhookOutbox:
    with session_scope() as s:
        return s.execute(select(WebhookOutbox).where(
            WebhookOutbox.id == job_id)).scalar_one()


def _enqueue(job_type="dispute_agent", payload=None, dedup_key=None) -> int:
    with session_scope() as s:
        job_id = enqueue_job(s, job_type, payload or {"dispute_id": "PP-D-1"},
                             dedup_key)
    assert job_id is not None
    return job_id


def _force_claimable(job_id: int) -> None:
    """Push next_attempt_at into the past (tests drive retries instantly)."""
    with session_scope() as s:
        s.execute(update(WebhookOutbox).where(WebhookOutbox.id == job_id)
                  .values(next_attempt_at=text("now() - interval '1 second'")))


def _make_worker(handler) -> Worker:
    return Worker(get_engine(), handler, FAST)


def _now_db() -> datetime:
    with session_scope() as s:
        return s.execute(text("SELECT now()")).scalar_one()


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------

def test_claim_marks_processing_and_counts_attempt():
    job_id = _enqueue()
    with session_scope() as s:
        claimed = claim_batch(s, 10)
    assert [j.id for j in claimed] == [job_id]
    row = _fetch(job_id)
    assert row.status == "processing"
    assert row.attempts == 1
    assert row.claimed_at is not None


def test_claim_skips_rows_locked_by_concurrent_claim():
    """SKIP LOCKED: while transaction A holds the claim lock, B sees zero
    rows (it does NOT block). After A commits, the row is processing."""
    job_id = _enqueue()
    with session_scope() as session_a:
        first = claim_batch(session_a, 10)  # locks the row, NOT committed
        assert [j.id for j in first] == [job_id]

        with session_scope() as s_b:  # concurrent claimer, A still open
            second = claim_batch(s_b, 10)
        assert second == []  # skipped, not blocked
    # session_a committed here: the row is now 'processing'

    with session_scope() as s_c:
        third = claim_batch(s_c, 10)
    assert third == []  # not pending anymore
    assert _fetch(job_id).status == "processing"


def test_claim_respects_next_attempt_at():
    future = _enqueue(dedup_key="future")
    due = _enqueue(dedup_key="due")
    with session_scope() as s:
        s.execute(update(WebhookOutbox).where(WebhookOutbox.id == future)
                  .values(next_attempt_at=text("now() + interval '1 hour'")))
    with session_scope() as s:
        claimed = claim_batch(s, 10)
    assert [j.id for j in claimed] == [due]


def test_claim_honors_batch_limit():
    ids = [_enqueue(dedup_key=f"b-{i}") for i in range(5)]
    with session_scope() as s:
        claimed = claim_batch(s, 2)
    assert len(claimed) == 2
    with session_scope() as s:
        rest = claim_batch(s, 10)
    assert len(rest) == 3
    assert set(j.id for j in claimed) | set(j.id for j in rest) == set(ids)


def test_enqueue_dedup_conflict_is_noop():
    with session_scope() as s:
        a = enqueue_job(s, "dispute_agent", {"n": 1}, "dispute:PP-D-9")
    with session_scope() as s:
        b = enqueue_job(s, "dispute_agent", {"n": 2}, "dispute:PP-D-9")
    assert a is not None and b is None  # second insert deduplicated
    with session_scope() as s:
        n = len(s.execute(select(WebhookOutbox)).scalars().all())
    assert n == 1


# ---------------------------------------------------------------------------
# run_once: the three finalize paths
# ---------------------------------------------------------------------------

def test_run_once_success_marks_processed():
    job_id = _enqueue()
    seen = []
    worker = _make_worker(lambda jt, p: seen.append((jt, p)))

    assert worker.run_once() == 1

    assert seen == [("dispute_agent", {"dispute_id": "PP-D-1"})]
    row = _fetch(job_id)
    assert row.status == "processed"
    assert row.processed_at is not None
    assert row.attempts == 1


def test_run_once_retryable_failure_reschedules_with_backoff():
    job_id = _enqueue()

    def flaky(job_type, payload):
        raise RetryableJobError("gateway 503")

    before = _now_db()
    _make_worker(flaky).run_once()

    row = _fetch(job_id)
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.claimed_at is None  # lease cleared
    assert "gateway 503" in row.last_error
    # Backoff pushed the retry into the future (base 0.02s, jitter >= 0.75x).
    assert row.next_attempt_at > before


def test_run_once_permanent_failure_dead_letters_immediately():
    job_id = _enqueue()

    def broken(job_type, payload):
        raise PermanentJobError("INVALID_API_KEY: agent deactivated")

    _make_worker(broken).run_once()

    row = _fetch(job_id)
    assert row.status == "dead"
    assert row.attempts == 1
    assert row.failed_at is not None
    assert "INVALID_API_KEY" in row.last_error


def test_malformed_payload_error_is_permanent():
    """classify() treats ValueError/KeyError as broken-job, not hiccup."""
    job_id = _enqueue()

    def bad_payload(job_type, payload):
        raise KeyError("dispute_id")  # payload shape is wrong; retry won't help

    _make_worker(bad_payload).run_once()
    assert _fetch(job_id).status == "dead"


def test_dead_after_max_attempts_with_reason():
    job_id = _enqueue()
    calls = []

    def always_flaky(job_type, payload):
        calls.append(1)
        raise RetryableJobError("still down")

    worker = _make_worker(always_flaky)
    for _ in range(FAST.max_attempts):
        worker.run_once()
        _force_claimable(job_id)  # skip the backoff wait
    worker.run_once()  # one more cycle: nothing due (row is dead)

    row = _fetch(job_id)
    assert row.status == "dead"
    assert row.attempts == FAST.max_attempts
    assert len(calls) == FAST.max_attempts  # never called again after death
    assert "still down" in row.last_error
    assert row.failed_at is not None


def test_backoff_is_exponential_jittered_and_capped():
    s = WorkerSettings(database_url="", backoff_base_seconds=1.0,
                       backoff_max_seconds=10.0)
    for attempt in range(1, 4):
        expected = min(10.0, 1.0 * 2 ** (attempt - 1))
        for _ in range(20):
            d = backoff_delay(attempt, s)
            assert expected * 0.75 <= d <= expected * 1.25
    # Cap: attempt 10 would be 512 raw -> clamped to 10 (+/- jitter).
    for _ in range(20):
        assert 7.5 <= backoff_delay(10, s) <= 12.5


# ---------------------------------------------------------------------------
# Lease-based crash recovery
# ---------------------------------------------------------------------------

def test_recover_stale_leases_reclaims_and_spares_fresh():
    stale = _enqueue(dedup_key="stale")
    fresh = _enqueue(dedup_key="fresh")
    with session_scope() as s:
        # Simulate a worker that died 10 minutes into a 5-minute lease.
        s.execute(update(WebhookOutbox).where(WebhookOutbox.id == stale)
                  .values(status="processing", attempts=1,
                          claimed_at=text("now() - interval '10 minutes'")))
        s.execute(update(WebhookOutbox).where(WebhookOutbox.id == fresh)
                  .values(status="processing", attempts=1,
                          claimed_at=text("now()")))
    with session_scope() as s:
        n = recover_stale_leases(s, lease_seconds=300.0, max_attempts=3)
    assert n == 1
    stale_row, fresh_row = _fetch(stale), _fetch(fresh)
    assert stale_row.status == "pending"
    assert stale_row.claimed_at is None
    assert "LEASE_EXPIRED" in stale_row.last_error
    assert fresh_row.status == "processing"  # untouched


def test_recovered_job_is_redispatched_and_completes():
    job_id = _enqueue()
    with session_scope() as s:
        s.execute(update(WebhookOutbox).where(WebhookOutbox.id == job_id)
                  .values(status="processing", attempts=1,
                          claimed_at=text("now() - interval '1 hour'")))
    done = []
    worker = _make_worker(lambda jt, p: done.append(jt))
    worker.run_once()  # sweep reclaims, same cycle claims + processes
    assert done == ["dispute_agent"]
    assert _fetch(job_id).status == "processed"


def test_recovery_dead_letters_exhausted_attempts():
    """A job that keeps crashing workers must not loop forever."""
    job_id = _enqueue()
    with session_scope() as s:
        s.execute(update(WebhookOutbox).where(WebhookOutbox.id == job_id)
                  .values(status="processing", attempts=FAST.max_attempts,
                          claimed_at=text("now() - interval '1 hour'")))
    with session_scope() as s:
        n = recover_stale_leases(s, lease_seconds=300.0,
                                 max_attempts=FAST.max_attempts)
    assert n == 1
    row = _fetch(job_id)
    assert row.status == "dead"
    assert row.failed_at is not None
    assert "exhausted" in row.last_error


# ---------------------------------------------------------------------------
# Concurrency: two workers, one job
# ---------------------------------------------------------------------------

def test_two_racing_workers_process_job_exactly_once():
    job_id = _enqueue()
    calls = []
    lock = threading.Lock()

    def slow(job_type, payload):
        with lock:
            calls.append(job_type)
        time.sleep(0.3)

    a, b = _make_worker(slow), _make_worker(slow)
    t1 = threading.Thread(target=a.run_once)
    t2 = threading.Thread(target=b.run_once)
    t1.start()
    time.sleep(0.05)  # let A win the claim first
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert calls == ["dispute_agent"]  # exactly once, despite the race
    assert _fetch(job_id).status == "processed"
