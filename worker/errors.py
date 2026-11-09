"""Job error taxonomy: retryable vs permanent.

The classification decides whether a failed attempt buys another try or
ends the job in the dead-letter view immediately. The default for UNKNOWN
exceptions is retryable — an unrecognized failure gets the benefit of the
doubt, bounded by max_attempts so 'benefit of the doubt' cannot mean
'forever'.
"""

from __future__ import annotations


class JobError(Exception):
    """Base for errors a handler raises on purpose."""


class RetryableJobError(JobError):
    """Transient failure — reschedule with backoff (network, 5xx, rate
    limits, a dependency that is down but expected to come back)."""


class PermanentJobError(JobError):
    """Definite failure — retrying cannot change the outcome (malformed
    payload, unknown job type, revoked credentials, a gateway policy
    rejection). Dead-letter immediately."""


# Exceptions that mean "the job itself is broken", not "the world hiccuped".
_PERMANENT_BUILTINS = (ValueError, KeyError, TypeError, AttributeError)


def classify(exc: BaseException) -> str:
    """Return 'permanent' or 'retryable' for a handler failure."""
    if isinstance(exc, PermanentJobError):
        return "permanent"
    if isinstance(exc, RetryableJobError):
        return "retryable"
    if isinstance(exc, _PERMANENT_BUILTINS):
        # Bad payload shape, missing key — the same input will fail identically.
        return "permanent"
    # httpx transport errors, PayPal server errors, unexpected internals:
    # retry, bounded by max_attempts.
    return "retryable"
