"""API routes: agent action authorization + approval lifecycle.

Endpoints
---------
POST /v1/agent-actions              the policy decision endpoint
GET  /v1/actions/{id}               action details (own agent only)
GET  /v1/approvals/{id}             approval + action (own agent only)
POST /v1/approvals/{id}/decision    approved | rejected   (operator)
POST /v1/approvals/{id}/execute     executed | failed     (post-PayPal-call)
POST /v1/admin/expire-approvals     run the expiry sweep now (normally cron)

Simplification to revisit before production: operator endpoints currently
authenticate with the same agent API keys. A real deployment needs a
separate operator role — an agent must not be able to approve its own
pending refunds. The state machine and audit trail are already correct;
only the identity check on /decision and /execute must change.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway import approvals as approvals_mod
from gateway.auth import AuthContext, enforce_rate_limit, get_db
from gateway.models import Action, Approval
from gateway.policy_engine import ActionRequest as EngineRequest
from gateway.policy_engine import authorize
from gateway.schemas import (
    ActionRequest,
    ApprovalDecisionRequest,
    DecisionResponse,
    ExecutionResultRequest,
)

router = APIRouter(prefix="/v1")


def _own_action(session: Session, ctx: AuthContext, action_id: int) -> Action:
    """Fetch an action, enforcing that it belongs to the calling agent.
    Cross-agent probes get 404 (not 403 — do not confirm the ID exists)."""
    action = session.execute(
        select(Action).where(Action.id == action_id, Action.agent_id == ctx.agent.id)
    ).scalar_one_or_none()
    if action is None:
        raise HTTPException(status_code=404, detail="action not found")
    return action


@router.post("/agent-actions", response_model=DecisionResponse)
def agent_actions(
    body: ActionRequest,
    ctx: AuthContext = Depends(enforce_rate_limit),
    session: Session = Depends(get_db),
):
    """Authorize one agent action against the agent's policy.

    All four outcomes (allowed/denied/pending_approval/deduplicated) return
    HTTP 200 — the decision is the payload. 4xx means the REQUEST failed
    (auth, validation, rate limit), not that the action was denied.
    """
    decision = authorize(
        session, ctx.agent, ctx.policy,
        EngineRequest(
            action_type=body.action_type,
            amount=body.amount,
            currency=body.currency,
            idempotency_key=body.idempotency_key,
            reference_id=body.reference_id,
        ),
    )
    # Commit before you speak: the client may immediately GET this action or
    # replay the idempotency key; teardown-commit could lose that race.
    session.commit()
    return DecisionResponse(
        outcome=decision.outcome,
        reason_code=decision.reason_code,
        action_id=decision.action_id,
        approval_id=decision.approval_id,
        duplicate_of_action_id=decision.duplicate_of_action_id,
        original_outcome=decision.original_outcome,
        rolling_24h_used=decision.rolling_24h_used,
        rolling_24h_cap=decision.rolling_24h_cap,
    )


@router.get("/actions/{action_id}")
def get_action(action_id: int,
               ctx: AuthContext = Depends(enforce_rate_limit),
               session: Session = Depends(get_db)):
    action = _own_action(session, ctx, action_id)
    return {
        "id": action.id, "action_type": action.action_type,
        "amount": str(action.amount), "currency": action.currency,
        "outcome": action.outcome, "reason_code": action.reason_code,
        "reference_id": action.reference_id,
        "idempotency_key": action.idempotency_key,
        "duplicate_of_action_id": action.duplicate_of_action_id,
        "created_at": action.created_at.isoformat(),
    }


def _own_approval(session: Session, ctx: AuthContext, approval_id: int) -> Approval:
    approval = session.execute(
        select(Approval).where(Approval.id == approval_id)
    ).scalar_one_or_none()
    if approval is None or approval.action.agent_id != ctx.agent.id:
        raise HTTPException(status_code=404, detail="approval not found")
    return approval


def _approval_view(approval: Approval) -> dict:
    return {
        "id": approval.id, "action_id": approval.action_id,
        "status": approval.status, "reason_code": approval.reason_code,
        "requested_at": approval.requested_at.isoformat(),
        "expires_at": approval.expires_at.isoformat(),
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "decided_by": approval.decided_by,
        "action": {
            "action_type": approval.action.action_type,
            "amount": str(approval.action.amount),
            "currency": approval.action.currency,
            "outcome": approval.action.outcome,
        },
    }


@router.get("/approvals/{approval_id}")
def get_approval(approval_id: int,
                 ctx: AuthContext = Depends(enforce_rate_limit),
                 session: Session = Depends(get_db)):
    return _approval_view(_own_approval(session, ctx, approval_id))


@router.post("/approvals/{approval_id}/decision")
def decide_approval(approval_id: int, body: ApprovalDecisionRequest,
                    ctx: AuthContext = Depends(enforce_rate_limit),
                    session: Session = Depends(get_db)):
    """Operator decision: pending → approved | rejected."""
    _own_approval(session, ctx, approval_id)  # ownership check
    try:
        approval = approvals_mod.decide(session, approval_id, body.decision, body.decided_by)
    except approvals_mod.InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()  # commit before you speak
    return _approval_view(approval)


@router.post("/approvals/{approval_id}/execute")
def execute_approval(approval_id: int, body: ExecutionResultRequest,
                     ctx: AuthContext = Depends(enforce_rate_limit),
                     session: Session = Depends(get_db)):
    """Record execution outcome after the PayPal refund call:
    approved → executed | failed."""
    _own_approval(session, ctx, approval_id)
    try:
        approval = approvals_mod.mark_execution(
            session, approval_id, body.executed, body.reason_code
        )
    except approvals_mod.InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()  # commit before you speak
    return _approval_view(approval)


@router.post("/admin/expire-approvals")
def expire_approvals(ctx: AuthContext = Depends(enforce_rate_limit),
                     session: Session = Depends(get_db)):
    """Run the expiry sweep now (pending approvals past expires_at → expired).
    Expiring releases each action's rolling-cap reservation."""
    expired = approvals_mod.expire_stale_approvals(session)
    session.commit()  # commit before you speak
    return {"expired_count": len(expired), "approval_ids": expired}
