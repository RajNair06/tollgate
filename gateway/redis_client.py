"""Redis-backed services: PayPal token cache and rate limiting.

Why Redis (vs the in-process cache in PayPalClient):
- The token cache is shared by ALL worker processes, so the fleet performs
  one OAuth exchange per ~9 hours, not one per worker per 9 hours.
- Rate-limit counters MUST be shared to mean anything: a per-process
  counter would multiply the effective limit by the worker count.

Failure policy — deliberate asymmetry:
- Token cache miss/error → fall back to fetching a token (correct, slower).
- Rate limiter Redis error → FAIL OPEN + log. Rate limiting is protection,
  not correctness: the per-agent SELECT FOR UPDATE lock and DB CHECK caps
  still enforce the money rules even if Redis is down. Failing closed would
  turn a cache outage into a full payments outage.
"""

from __future__ import annotations

import logging
import time

import redis

from config import get_settings

logger = logging.getLogger(__name__)

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """Process-wide Redis client (lazy)."""
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = redis.Redis.from_url(
            settings.redis_url, decode_responses=True, socket_timeout=2,
            socket_connect_timeout=2,
        )
    return _redis


# ---------------------------------------------------------------------------
# PayPal OAuth token cache
# ---------------------------------------------------------------------------

class RedisTokenCache:
    """Drop-in token cache for PayPalClient.

    Interface (all PayPalClient needs):
      get() -> str | None            current valid token, or None
      set(token, expires_in) -> None store with a TTL that includes the
                                     client's expiry margin semantics.

    Redis SET with EX gives expiration for free — no clock math, no stale
    entries: the key simply vanishes when its TTL ends.
    """

    KEY = "paypal:access_token"
    MARGIN_SECONDS = 60  # retire the cached token 60s before real expiry

    def __init__(self, r: redis.Redis | None = None):
        self._r = r or get_redis()

    def get(self) -> str | None:
        try:
            return self._r.get(self.KEY)
        except redis.RedisError as exc:
            logger.warning("Redis token-cache read failed (%s); fetching fresh token", exc)
            return None

    def set(self, token: str, expires_in: float) -> None:
        ttl = max(int(expires_in) - self.MARGIN_SECONDS, 1)
        try:
            self._r.set(self.KEY, token, ex=ttl)
        except redis.RedisError as exc:
            logger.warning("Redis token-cache write failed (%s); continuing uncached", exc)

    def clear(self) -> None:
        try:
            self._r.delete(self.KEY)
        except redis.RedisError:
            pass


# ---------------------------------------------------------------------------
# Rate limiting — fixed-window counters
# ---------------------------------------------------------------------------

class RateLimitExceeded(Exception):
    """Raised when a request exceeds per-agent or global limits."""

    def __init__(self, scope: str, limit: int, retry_after_seconds: int):
        self.scope = scope            # "agent" | "global"
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"{scope} rate limit exceeded ({limit}/min)")


def _window_key(prefix: str) -> tuple[str, int]:
    """Fixed 60-second window key + seconds until the window rolls over."""
    bucket = int(time.time() // 60)
    return f"rl:{prefix}:{bucket}", 60 - int(time.time() % 60)


def _check(r: redis.Redis, prefix: str, limit: int, scope: str) -> None:
    key, retry_after = _window_key(prefix)
    # INCR + EXPIRE(NX): counter created on first hit, TTL set exactly once.
    pipe = r.pipeline(transaction=True)
    pipe.incr(key)
    pipe.expire(key, 120, nx=True)  # outlive the window; auto-cleanup
    count, _ = pipe.execute()
    if int(count) > limit:
        raise RateLimitExceeded(scope=scope, limit=limit, retry_after_seconds=retry_after)


def check_rate_limits(agent_id: int) -> None:
    """Enforce per-agent AND global limits. Raises RateLimitExceeded.

    Called AFTER authentication (we know the agent) and BEFORE the policy
    engine (cheap rejection before expensive DB work).
    """
    settings = get_settings()
    try:
        r = get_redis()
        _check(r, f"agent:{agent_id}", settings.rate_limit_per_agent_per_min, "agent")
        _check(r, "global", settings.rate_limit_global_per_min, "global")
    except redis.RedisError as exc:
        logger.error("Rate limiter unavailable (%s) — FAILING OPEN", exc)
