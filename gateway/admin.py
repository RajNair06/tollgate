"""Admin provisioning: create agents, issue keys, rotate (revoke) keys.

Trust model
-----------
Admin routes are guarded by ADMIN_API_KEY (env var, X-Admin-Key header) —
deliberately NOT an agent key. Root trust must come from deployment config:
you cannot create the admin credential through the admin API itself. If the
env var is unset, every admin route returns 503 (fail-safe disabled).

The same separation the approval endpoints still need (agents must not
approve their own refunds) is already enforced here: agent keys are simply
not accepted on /v1/admin/*.

Key lifecycle
-------------
- POST /v1/admin/agents               agent + policy + first key
- POST /v1/admin/agents/{id}/keys     additional key (rotation: issue new,
                                      migrate the agent, then revoke old)
- DELETE /v1/admin/keys/{key_id}      soft revoke (revoked_at set)
- GET  /v1/admin/agents[/{id}]        ops views (prefixes only, never hashes)

The plaintext key appears exactly once, in the response that created it.
Only the SHA-256 hash is stored; retrieval is impossible by design.

Everything is audited (agent_created / key_issued / key_revoked) in the same
transaction as the change, and — because auth does a fresh indexed lookup
per request — new keys work and revoked keys die on the RUNNING server with
no restart and no cache invalidation.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import get_settings
from gateway import audit
from gateway.api_keys import generate_api_key
from gateway.auth import get_db
from gateway.models import Agent, AgentKey, Policy
from gateway.schemas import (
    AgentAdminView,
    AgentKeyView,
    CreateAgentRequest,
    CreateAgentResponse,
    IssueKeyResponse,
    RevokeKeyResponse,
)

logger = logging.getLogger(__name__)

admin_router = APIRouter(prefix="/v1/admin")


def require_admin(
    session: Session = Depends(get_db),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> None:
    """Validate the bootstrap admin credential. Fail-safe: unset env var
    disables every admin route; wrong key is audited like any auth failure."""
    expected = get_settings().admin_api_key
    if not expected:
        raise HTTPException(
            status_code=503,
            detail={"reason_code": "ADMIN_DISABLED"},
        )
    if not x_admin_key or not hmac.compare_digest(
        x_admin_key.encode("utf-8"), expected.encode("utf-8")
    ):
        audit.record(session, decision="auth_failed", reason_code="INVALID_ADMIN_KEY",
                     detail={"path_hint": "admin"})
        session.commit()  # survive the request rollback — same rule as agent auth
        raise HTTPException(status_code=401,
                            detail={"reason_code": "INVALID_ADMIN_KEY"})


def _key_view(k: AgentKey) -> AgentKeyView:
    return AgentKeyView(
        id=k.id, key_prefix=k.key_prefix, label=k.label,
        created_at=k.created_at.isoformat(),
        revoked_at=k.revoked_at.isoformat() if k.revoked_at else None,
    )


def _agent_view(agent: Agent) -> AgentAdminView:
    return AgentAdminView(
        id=agent.id, name=agent.name, is_active=agent.is_active,
        created_at=agent.created_at.isoformat(),
        policy={
            "allowed_actions": agent.policy.allowed_actions,
            "per_txn_cap": str(agent.policy.per_txn_cap),
            "rolling_24h_cap": str(agent.policy.rolling_24h_cap),
            "approval_threshold": str(agent.policy.approval_threshold),
            "approval_ttl_minutes": agent.policy.approval_ttl_minutes,
        },
        keys=[_key_view(k) for k in agent.keys],
    )


@admin_router.post("/agents", response_model=CreateAgentResponse)
def create_agent(
    body: CreateAgentRequest,
    _: None = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Provision agent + policy + first API key. The plaintext key is
    returned ONCE and can never be retrieved again."""
    plaintext, key_hash, prefix = generate_api_key()

    agent = Agent(name=body.name)
    session.add(agent)
    try:
        session.flush()  # surface the UNIQUE(name) violation here, with context
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409,
                            detail={"reason_code": "AGENT_NAME_TAKEN",
                                    "name": body.name}) from exc

    session.add(Policy(
        agent_id=agent.id,
        allowed_actions=body.allowed_actions,
        per_txn_cap=body.per_txn_cap,
        rolling_24h_cap=body.rolling_24h_cap,
        approval_threshold=body.approval_threshold,
        approval_ttl_minutes=body.approval_ttl_minutes,
    ))
    key = AgentKey(agent_id=agent.id, key_hash=key_hash, key_prefix=prefix,
                   label=body.key_label)
    session.add(key)
    session.flush()

    audit.record(session, decision="agent_created", reason_code="OK",
                 agent_id=agent.id,
                 detail={"name": agent.name, "key_id": key.id,
                         "key_prefix": prefix,
                         "per_txn_cap": str(body.per_txn_cap),
                         "rolling_24h_cap": str(body.rolling_24h_cap),
                         "approval_threshold": str(body.approval_threshold),
                         "allowed_actions": body.allowed_actions})
    # COMMIT BEFORE YOU SPEAK: the response hands the caller a live credential.
    # Dependency teardown would commit too late — a client that uses the key on
    # its very next request could outrun the commit and get a spurious 401
    # (observed: 17ms race). Commit here; session_scope teardown is a no-op.
    session.commit()
    logger.info("Admin created agent %s (id=%s, key=%s...)",
                agent.name, agent.id, prefix)
    return CreateAgentResponse(agent_id=agent.id, name=agent.name,
                               key_id=key.id, key_prefix=prefix,
                               api_key=plaintext)


@admin_router.post("/agents/{agent_id}/keys", response_model=IssueKeyResponse)
def issue_key(
    agent_id: int,
    label: str | None = None,
    _: None = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Issue an ADDITIONAL key for rotation: both old and new keys stay valid
    until the old one is revoked — zero-downtime cutover."""
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail={"reason_code": "AGENT_NOT_FOUND"})

    plaintext, key_hash, prefix = generate_api_key()
    key = AgentKey(agent_id=agent.id, key_hash=key_hash, key_prefix=prefix,
                   label=label)
    session.add(key)
    session.flush()

    audit.record(session, decision="key_issued", reason_code="OK",
                 agent_id=agent.id,
                 detail={"key_id": key.id, "key_prefix": prefix, "label": label})
    session.commit()  # commit before you speak — see create_agent
    logger.info("Admin issued key %s... for agent %s", prefix, agent.id)
    return IssueKeyResponse(agent_id=agent.id, key_id=key.id,
                            key_prefix=prefix, api_key=plaintext)


@admin_router.delete("/keys/{key_id}", response_model=RevokeKeyResponse)
def revoke_key(
    key_id: int,
    _: None = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Soft-revoke a key. Idempotent: revoking twice reports already_revoked.
    Takes effect on the running server immediately (auth reads the DB per
    request); the row survives for forensics."""
    key = session.execute(
        select(AgentKey).where(AgentKey.id == key_id).with_for_update()
    ).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail={"reason_code": "KEY_NOT_FOUND"})

    already = key.revoked_at is not None
    if not already:
        key.revoked_at = func.now()
        session.flush()
        audit.record(session, decision="key_revoked", reason_code="OK",
                     agent_id=key.agent_id,
                     detail={"key_id": key.id, "key_prefix": key.key_prefix})
        # Commit before you speak — security-critical direction of the race:
        # a "revoked" response must never reach the client while the key
        # still works on the next request.
        session.commit()
        logger.info("Admin revoked key %s (agent %s)", key.key_prefix, key.agent_id)

    # After flush, accessing key.revoked_at re-fetches the server-computed
    # timestamp (assigning a SQL expression expires the attribute).
    return RevokeKeyResponse(
        key_id=key.id, agent_id=key.agent_id, key_prefix=key.key_prefix,
        revoked_at=key.revoked_at.isoformat(),
        already_revoked=already,
    )


@admin_router.get("/agents", response_model=list[AgentAdminView])
def list_agents(
    _: None = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Ops overview: every agent with policy and key prefixes (never hashes)."""
    agents = session.execute(
        select(Agent).order_by(Agent.id)
    ).scalars().unique().all()
    # Trigger lazy loads while the session is open.
    for a in agents:
        _ = a.policy, a.keys
    return [_agent_view(a) for a in agents]


@admin_router.get("/agents/{agent_id}", response_model=AgentAdminView)
def get_agent(
    agent_id: int,
    _: None = Depends(require_admin),
    session: Session = Depends(get_db),
):
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail={"reason_code": "AGENT_NOT_FOUND"})
    _ = agent.policy, agent.keys
    return _agent_view(agent)
