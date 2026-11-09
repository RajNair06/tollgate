"""The worker loop: claim → dispatch → bookkeeping.

run_once() is one full poll cycle and is the unit tests drive:
1. SHORT claim transaction: claim up to batch_size due jobs (SKIP LOCKED),
   flip them to 'processing', commit. After the commit nothing is locked —
   the lease protects the work phase.
2. For each claimed job, call the handler OUTSIDE any transaction.
3. SHORT finalize transaction per job:
   - handler returned          -> processed
   - permanent error           -> dead (last_error + failed_at)
   - retryable error, attempts
     below max                 -> pending again, next_attempt_at pushed out
     exponentially with jitter
   - retryable error, attempts
     at max                    -> dead

run_forever() adds the idle sleep, the periodic lease sweep (crash
recovery), and cooperative shutdown via a threading.Event.

The handler contract: handler(job_type: str, payload: dict) -> None.
Return normally on success; raise worker.errors.PermanentJobError /
RetryableJobError to choose the failure path explicitly; any OTHER
exception is classified by worker.errors.classify (unknown = retryable,
bounded by max_attempts).
"""

from __future__ import annotations

import logging
import random
import threading
from typing import Callable

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from worker.config import WorkerSettings
from worker.errors import classify
from worker.queue import (
    claim_batch,
    mark_dead,
    mark_processed,
    recover_stale_leases,
    reschedule,
)

logger = logging.getLogger(__name__)

Handler = Callable[[str, dict], None]

# Jitter bounds for backoff: [0.75x, 1.25x] of the computed delay.
_JITTER_LO, _JITTER_HI = 0.75, 1.25

# Truncate recorded errors so a pathological traceback cannot blow up the row.
_MAX_ERROR_LEN = 2000


def backoff_delay(attempts: int, settings: WorkerSettings) -> float:
    """Exponential backoff with jitter.

    attempts is the count AFTER the failed attempt's claim increment, so
    the first failure (attempts=1) waits ~base, the second ~2*base, ...
    capped at backoff_max_seconds. Jitter desynchronizes parallel workers
    retrying the same downstream outage.
    """
    raw = min(settings.backoff_max_seconds,
              settings.backoff_base_seconds * (2 ** (attempts - 1)))
    return raw * random.uniform(_JITTER_LO, _JITTER_HI)


def _error_message(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_LEN]


class Worker:
    """Owns its engine and session factory — the worker process must not
    depend on gateway.db's process-wide engine (that one is built from the
    gateway's settings, which require PayPal credentials the worker never
    needs). pool_pre_ping replaces connections dropped by a DB restart."""

    def __init__(self, engine: Engine, handler: Handler,
                 settings: WorkerSettings):
        self._engine = engine
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._handler = handler
        self.settings = settings

    @classmethod
    def from_settings(cls, settings: WorkerSettings,
                      handler: Handler) -> "Worker":
        engine = create_engine(settings.database_url, pool_size=2,
                               max_overflow=3, pool_pre_ping=True)
        return cls(engine, handler, settings)

    def close(self) -> None:
        self._engine.dispose()

    # -- session helper -------------------------------------------------------

    def _scope(self) -> "_WorkerScope":
        return _WorkerScope(self._sessions)

    # -- one poll cycle -------------------------------------------------------

    def run_once(self) -> int:
        """Claim and process one batch. Returns the number of jobs claimed."""
        with self._scope() as s:
            recovered = recover_stale_leases(
                s, self.settings.lease_seconds, self.settings.max_attempts)
            if recovered:
                logger.warning("Lease sweep reclaimed %d stale job(s)", recovered)
            claimed = claim_batch(s, self.settings.batch_size)
            # Detach the dispatch inputs before the claim transaction commits.
            batch = [(job.id, job.job_type, dict(job.payload), job.attempts)
                     for job in claimed]

        for job_id, job_type, payload, attempts in batch:
            self._dispatch(job_id, job_type, payload, attempts)
        return len(batch)

    def _dispatch(self, job_id: int, job_type: str, payload: dict,
                  attempts: int) -> None:
        try:
            self._handler(job_type, payload)
        except Exception as exc:  # noqa: BLE001 — bookkeeping must survive any handler
            self._finalize_failure(job_id, attempts, exc)
        else:
            with self._scope() as s:
                mark_processed(s, job_id)
            logger.info("Job %d (%s) processed after %d attempt(s)",
                        job_id, job_type, attempts)

    def _finalize_failure(self, job_id: int, attempts: int,
                          exc: BaseException) -> None:
        kind = classify(exc)
        message = _error_message(exc)
        exhausted = attempts >= self.settings.max_attempts
        with self._scope() as s:
            if kind == "permanent" or exhausted:
                mark_dead(s, job_id, message)
            else:
                delay = backoff_delay(attempts, self.settings)
                reschedule(s, job_id, delay, message)
        if kind == "permanent" or exhausted:
            logger.error("Job %d dead-lettered after %d attempt(s): %s "
                         "(%s)", job_id, attempts, message,
                         "permanent error" if kind == "permanent"
                         else "attempts exhausted")
        else:
            logger.warning("Job %d attempt %d/%d failed (%s); retry in %.1fs: %s",
                           job_id, attempts, self.settings.max_attempts,
                           kind, delay, message)

    # -- the loop ---------------------------------------------------------------

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        logger.info("Worker started (max_attempts=%d, lease=%.0fs, batch=%d)",
                    self.settings.max_attempts, self.settings.lease_seconds,
                    self.settings.batch_size)
        while not stop.is_set():
            try:
                processed = self.run_once()
            except Exception:  # noqa: BLE001 — a broken cycle must not kill the worker
                logger.exception("Poll cycle failed; backing off")
                stop.wait(self.settings.poll_interval_seconds)
                continue
            if processed == 0:
                stop.wait(self.settings.poll_interval_seconds)
        logger.info("Worker stopped")


class _WorkerScope:
    """Commit-on-clean-exit session scope bound to the worker's sessionmaker."""

    def __init__(self, factory):
        self._factory = factory
        self._session: Session | None = None

    def __enter__(self) -> Session:
        self._session = self._factory()
        return self._session

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self._session is not None
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
