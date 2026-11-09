"""Worker configuration — environment-driven, fail-loud validation.

Deliberately does NOT reuse config.get_settings(): that requires PayPal
credentials, and the worker has no business holding them. The worker needs
exactly one secret: the database URL where the job queue lives.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerSettings:
    database_url: str
    # A job is dead-lettered once its attempt count reaches this.
    max_attempts: int = 5
    # Exponential backoff: delay = min(backoff_max, base * 2^(attempts-1)),
    # multiplied by jitter in [0.75, 1.25] so parallel workers desynchronize.
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 300.0
    # A 'processing' row whose claimed_at is older than this is presumed
    # crashed and reclaimed (or dead-lettered if attempts are exhausted).
    lease_seconds: float = 300.0
    # Idle sleep between polls, and jobs claimed per poll.
    poll_interval_seconds: float = 1.0
    batch_size: int = 5


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = float(raw)  # ValueError on garbage — fail loudly at startup
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")
    return value


def get_worker_settings() -> WorkerSettings:
    max_attempts = int(os.environ.get("WORKER_MAX_ATTEMPTS", "5"))
    if max_attempts < 1:
        raise ValueError(f"WORKER_MAX_ATTEMPTS must be >= 1, got {max_attempts}")
    return WorkerSettings(
        # The queue lives in the gateway database.
        database_url=os.environ.get(
            "WORKER_DATABASE_URL",
            os.environ.get(
                "DATABASE_URL",
                "postgresql+psycopg://tollgate:tollgate@localhost:5433/tollgate",
            ),
        ).strip(),
        max_attempts=max_attempts,
        backoff_base_seconds=_positive_float("WORKER_BACKOFF_BASE_SECONDS", 2.0),
        backoff_max_seconds=_positive_float("WORKER_BACKOFF_MAX_SECONDS", 300.0),
        lease_seconds=_positive_float("WORKER_LEASE_SECONDS", 300.0),
        poll_interval_seconds=_positive_float("WORKER_POLL_INTERVAL_SECONDS", 1.0),
        batch_size=int(os.environ.get("WORKER_BATCH_SIZE", "5")),
    )
