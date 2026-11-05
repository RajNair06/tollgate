"""Authentication and rate-limit dependencies for FastAPI.

Auth flow (per request):
  1. Extract key: X-API-Key header, or Authorization: Bearer tg_...
  2. SHA-256 the key → single indexed lookup on agents.api_key_hash.
     The plaintext key never touches the database or the logs.
  3. Resolve agent + policy (1:1). Inactive agent or missing policy → reject.
  4. Every rejection is AUDITED (committed immediately — the request's
     transaction will roll back, but the audit trail of failures must not).
  5. Rate limits (Redis, shared across workers): per-agent window, then
     global window. Exceeded → 429 + audit.

The resolved identity (AuthContext) flows to the route handler, which never
sees the API key again — key handling is confined to this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway import audit
from gateway.api_keys import hash_api_key
from gateway.db import session_scope
from gateway.models import Agent, Policy
from gateway.redis_client import RateLimitExceeded, check_rate_limits

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthContext:
    agent: Agent
    policy: Policy


def get_db() -> Iterator[Session]:
    """One session per request, one transaction: commit when the handler
    returns cleanly, rollback on any exception (including HTTPException)."""
    with session_scope() as session:
        yield session


def _extract_key(x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _audit_and_reject(session: Session, status: int, reason: str,
                      detail: dict, agent_id: int | None = None) -> HTTPException:
    """Commit the failure audit immediately, then build the HTTP error.

    The commit is essential: raising HTTPException unwinds the request
    transaction, which would otherwise erase evidence of the failure.
    """
    decision = "rate_limited" if reason.startswith("RATE_LIMIT") else "auth_failed"
    audit.record(session, decision=decision, reason_code=reason,
                 agent_id=agent_id, detail=detail)
    session.commit()
    headers = {}
    if status == 429 and "retry_after" in detail:
        headers["Retry-After"] = str(detail["retry_after"])
    return HTTPException(status_code=status, detail={"reason_code": reason}, headers=headers)


def get_auth_context(
    session: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> AuthContext:
    """Resolve the API key to (agent, policy) or reject with an audited 401/403."""
    key = _extract_key(x_api_key, authorization)
    if not key:
        raise _audit_and_reject(session, 401, "MISSING_API_KEY",
                                {"hint": "send X-API-Key or Authorization: Bearer"})

    agent = session.execute(
        select(Agent).where(Agent.api_key_hash == hash_api_key(key))
    ).scalar_one_or_none()

    if agent is None:
        # Do not echo the key or its hash in the audit row — hashes of
        # low-entropy guesses are still guessable. Prefix is enough.
        raise _audit_and_reject(session, 401, "INVALID_API_KEY",
                                {"key_prefix": key[:11]})
    if not agent.is_active:
        raise _audit_and_reject(session, 403, "AGENT_INACTIVE",
                                {"agent_id": agent.id}, agent_id=agent.id)

    policy = session.execute(
        select(Policy).where(Policy.agent_id == agent.id)
    ).scalar_one_or_none()
    if policy is None:
        # Provisioning bug: agents are created WITH a policy. Fail closed.
        logger.error("Agent %s has no policy row — failing closed", agent.id)
        raise _audit_and_reject(session, 403, "POLICY_MISSING",
                                {"agent_id": agent.id}, agent_id=agent.id)

    return AuthContext(agent=agent, policy=policy)


def enforce_rate_limit(
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_db),
) -> AuthContext:
    """Per-agent + global fixed-window limits in Redis. Runs AFTER auth
    (needs agent identity) and BEFORE the policy engine (cheap rejection)."""
    try:
        check_rate_limits(ctx.agent.id)
    except RateLimitExceeded as exc:
        raise _audit_and_reject(
            session, 429, "RATE_LIMIT_EXCEEDED",
            {"scope": exc.scope, "limit_per_min": exc.limit,
             "retry_after": exc.retry_after_seconds},
            agent_id=ctx.agent.id,
        ) from exc
    return ctx
