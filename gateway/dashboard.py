"""Read-only dashboard APIs — the data plane behind /dashboard.

Five endpoints, all X-Admin-Key gated (router-level dependency), all
designed for a polling frontend (2s cadence): cheap, bounded by limit
params, newest-first, and FULL-FIDELITY: nothing is truncated server-side
(reasoning, evidence, last_error are returned whole — wrapping/clamping
is a presentation decision that belongs to the dashboard, and its rule is
"wrap inside the cell, never cut off").

The approvals endpoint performs a CROSS-DATABASE join in Python:
approvals live in the gateway DB, the agent's reasoning lives in
agent_runs (agent DB) linked by approval_id. No FK may cross that
boundary — the gateway must not depend on a schema another process
owns — so the join is two queries plus a dict merge. If the agent DB is
unreachable the endpoints DEGRADE (agent section null) instead of
failing: a dashboard that cannot show reasoning must still show money.
"""

from __future__ import annotations

import logging
from datetime import datetime

import psycopg
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config import get_settings
from gateway.admin import require_admin
from gateway.auth import get_db
from gateway.models import (
    Action,
    Agent,
    Approval,
    AuditLog,
    WebhookEvent,
    WebhookOutbox,
)

logger = logging.getLogger(__name__)

dashboard_router = APIRouter(
    prefix="/v1/admin/dashboard",
    dependencies=[Depends(require_admin)],
)


# ---------------------------------------------------------------------------
# Agent-database reads (cross-DB half of the join)
# ---------------------------------------------------------------------------

def _agent_query(sql: str, params: dict | None = None) -> list[dict] | None:
    """Run a read query against the AGENT database. Returns None when the
    agent DB is unreachable — callers degrade rather than fail."""
    url = get_settings().agent_database_url
    if not url:
        return None
    try:
        with psycopg.connect(url, autocommit=True, prepare_threshold=0,
                             connect_timeout=3,
                             row_factory=psycopg.rows.dict_row) as conn:
            rows = conn.execute(sql, params or {}).fetchall()
        return [dict(r) for r in rows]
    except psycopg.Error:
        logger.exception("agent database read failed — degrading to gateway-only view")
        return None


def _agent_runs_by_approval(approval_ids: list[int]) -> dict[int, dict]:
    """approval_id -> the agent's side of the story (reasoning included)."""
    rows = _agent_query(
        "SELECT approval_id, run_id, dispute_id, status, summary, "
        "       confidence, proposed_action, detail "
        "FROM agent_runs WHERE approval_id = ANY(%(ids)s)",
        {"ids": approval_ids},
    )
    if rows is None:
        return {}
    merged: dict[int, dict] = {}
    for r in rows:
        detail = r.get("detail") or {}
        if isinstance(detail, str):  # defensive: JSON column normally decodes
            import json as _json
            try:
                detail = _json.loads(detail)
            except ValueError:
                detail = {}
        decision = detail.get("decision") or {}
        merged[r["approval_id"]] = {
            "run_id": r["run_id"],
            "dispute_id": r["dispute_id"],
            "status": r["status"],
            "summary": r["summary"],
            "confidence": r["confidence"],
            "proposed_action": r["proposed_action"],
            "reasoning": decision.get("reasoning"),
            "cited_evidence": decision.get("cited_evidence") or [],
        }
    return merged


# ---------------------------------------------------------------------------
# 1. Action feed — every authorization request, newest first
# ---------------------------------------------------------------------------

@dashboard_router.get("/actions")
def recent_actions(
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_db),
):
    rows = session.execute(
        select(Action, Agent.name)
        .join(Agent, Agent.id == Action.agent_id)
        .order_by(Action.id.desc())
        .limit(limit)
    ).all()
    return {"actions": [{
        "id": a.id,
        "agent_id": a.agent_id,
        "agent_name": name,
        "action_type": a.action_type,
        "amount": str(a.amount),
        "currency": a.currency,
        "outcome": a.outcome,
        "reason_code": a.reason_code,
        "idempotency_key": a.idempotency_key,
        "reference_id": a.reference_id,
        "created_at": a.created_at.isoformat(),
        "execution_failed_at": (a.execution_failed_at.isoformat()
                                if a.execution_failed_at else None),
    } for a, name in rows]}


# ---------------------------------------------------------------------------
# 2. Audit log — filterable by decision, agent, action type, time window
# ---------------------------------------------------------------------------

@dashboard_router.get("/audit")
def audit_feed(
    limit: int = Query(100, ge=1, le=1000),
    decision: str | None = Query(None),
    agent_id: int | None = Query(None),
    action_type: str | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    session: Session = Depends(get_db),
):
    stmt = (
        select(AuditLog, Agent.name, Action.action_type)
        .outerjoin(Agent, Agent.id == AuditLog.agent_id)
        .outerjoin(Action, Action.id == AuditLog.action_id)
    )
    if decision:
        stmt = stmt.where(AuditLog.decision == decision)
    if agent_id is not None:
        stmt = stmt.where(AuditLog.agent_id == agent_id)
    if action_type:
        stmt = stmt.where(Action.action_type == action_type)
    if since is not None:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.created_at <= until)
    rows = session.execute(
        stmt.order_by(AuditLog.id.desc()).limit(limit)
    ).all()
    return {"audit": [{
        "id": r.id,
        "decision": r.decision,
        "reason_code": r.reason_code,
        "agent_id": r.agent_id,
        "agent_name": name,
        "action_id": r.action_id,
        "action_type": atype,
        "amount": str(r.amount) if r.amount is not None else None,
        "detail": r.detail,
        "created_at": r.created_at.isoformat(),
    } for r, name, atype in rows]}


# ---------------------------------------------------------------------------
# 3. Approvals — pending queue with the agent's reasoning (cross-DB join)
# ---------------------------------------------------------------------------

@dashboard_router.get("/approvals")
def approvals_view(
    status: str = Query("pending"),
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_db),
):
    """status: pending (default) | approved | rejected | executed | failed |
    expired | all. Each row carries the gateway facts (amount, agent, TTL)
    plus the agent's side — reasoning, cited evidence, confidence — or
    agent: null when no run is linked (or the agent DB is down)."""
    stmt = (
        select(Approval, Action, Agent.name)
        .join(Action, Action.id == Approval.action_id)
        .join(Agent, Agent.id == Action.agent_id)
    )
    if status != "all":
        stmt = stmt.where(Approval.status == status)
    rows = session.execute(
        stmt.order_by(Approval.id.desc()).limit(limit)
    ).all()

    approval_ids = [ap.id for ap, _, _ in rows]
    agent_side = _agent_runs_by_approval(approval_ids) if approval_ids else {}

    return {"approvals": [{
        "approval_id": ap.id,
        "status": ap.status,
        "reason_code": ap.reason_code,
        "requested_at": ap.requested_at.isoformat(),
        "expires_at": ap.expires_at.isoformat() if ap.expires_at else None,
        "decided_at": ap.decided_at.isoformat() if ap.decided_at else None,
        "decided_by": ap.decided_by,
        "action": {
            "id": act.id,
            "action_type": act.action_type,
            "amount": str(act.amount),
            "currency": act.currency,
            "outcome": act.outcome,
            "reason_code": act.reason_code,
        },
        "agent_name": name,
        "agent": agent_side.get(ap.id),
    } for ap, act, name in rows]}


# ---------------------------------------------------------------------------
# 4. Metrics — one-shot numbers for the dashboard strip
# ---------------------------------------------------------------------------

@dashboard_router.get("/metrics")
def metrics(session: Session = Depends(get_db)):
    action_counts = dict(session.execute(
        select(Action.outcome, func.count()).group_by(Action.outcome)
    ).all())
    approval_counts = dict(session.execute(
        select(Approval.status, func.count()).group_by(Approval.status)
    ).all())
    outbox_counts = dict(session.execute(
        select(WebhookOutbox.status, func.count())
        .group_by(WebhookOutbox.status)
    ).all())
    webhook_events_total = session.execute(
        select(func.count()).select_from(WebhookEvent)
    ).scalar_one()

    agent_rows = _agent_query(
        "SELECT count(*) AS runs_total, "
        "       count(*) FILTER (WHERE status = 'pending_approval') AS awaiting_approval, "
        "       count(*) FILTER (WHERE status IN ('refund_executed', "
        "             'refund_executed_after_approval')) AS refunds_executed, "
        "       percentile_cont(0.5) WITHIN GROUP (ORDER BY "
        "             EXTRACT(EPOCH FROM (updated_at - created_at))) "
        "           FILTER (WHERE status NOT IN ('pending_approval', 'running')) "
        "           AS median_run_seconds, "
        "       coalesce(sum(total_tokens), 0) AS total_tokens, "
        "       coalesce(sum(cost_usd), 0) AS total_cost_usd "
        "FROM agent_runs"
    )
    agent_metrics = agent_rows[0] if agent_rows else None

    return {
        "actions": {
            "total": sum(action_counts.values()),
            "by_outcome": action_counts,
            # "blocked" = the gateway said no (caps, allowlist, inactive).
            "denied": action_counts.get("denied", 0),
            # Duplicate requests absorbed by idempotency — replays that
            # returned the original verdict instead of moving money twice.
            "duplicates_absorbed": action_counts.get("deduplicated", 0),
        },
        "approvals": {
            "total": sum(approval_counts.values()),
            "by_status": approval_counts,
            "pending": approval_counts.get("pending", 0),
        },
        "webhooks": {
            "events_received": webhook_events_total,
            "jobs_by_status": outbox_counts,
            "dead_letter": outbox_counts.get("dead", 0),
        },
        "agent": agent_metrics,  # null when the agent DB is unreachable
    }


# ---------------------------------------------------------------------------
# 5. Dead letter — jobs that exhausted retries (full error text, uncut)
# ---------------------------------------------------------------------------

@dashboard_router.get("/dead-letter")
def dead_letter(
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_db),
):
    rows = session.execute(
        select(WebhookOutbox)
        .where(WebhookOutbox.status == "dead")
        .order_by(WebhookOutbox.failed_at.desc().nullslast(),
                  WebhookOutbox.id.desc())
        .limit(limit)
    ).scalars().all()
    return {"dead_letter": [{
        "id": j.id,
        "job_type": j.job_type,
        "dedup_key": j.dedup_key,
        "payload": j.payload,
        "attempts": j.attempts,
        # FULL error text — the dashboard wraps it inside the cell; nothing
        # is cut off server-side.
        "last_error": j.last_error,
        "failed_at": j.failed_at.isoformat() if j.failed_at else None,
        "created_at": j.created_at.isoformat(),
    } for j in rows]}
