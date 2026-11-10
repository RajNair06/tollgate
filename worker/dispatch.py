"""Job dispatch: map outbox rows onto agent work, audit outcomes, classify.

The classification mirrors the gateway's definite-vs-ambiguous failure
philosophy (rolling-cap release): a run is only retried when the evidence
says the failure was TRANSIENT. Definite failures dead-letter immediately
with the reason recorded; completed verdicts — including verdicts we don't
like (denied, schema_failed) — are PROCESSED, because the pipeline's job
was to produce a recorded verdict, and it did. (rejected_by_critic is the
one exception: the draft is a single LLM sample, so the worker resamples
it within the attempts budget — every money gate re-runs per sample.)

Every dispatched run's final outcome is written to the gateway's audit_log
(decision='agent_run_finished') BEFORE the job is finalized, so a retry
after an audit-write failure cannot skip the audit: the runner's checkpoint
pre-check makes the re-run a cheap no-op, and the audit row lands then.
"""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from gateway.api_keys import hash_api_key
from gateway.audit import record
from gateway.models import AgentKey
from worker.errors import PermanentJobError, RetryableJobError

logger = logging.getLogger(__name__)

# The run reached a recorded verdict. The job did its job.
_PROCESSED_STATUSES = frozenset({
    "refund_executed", "refund_executed_after_approval",
    "refund_denied_by_policy", "refund_deduplicated",
    "decision_deny_refund", "decision_escalate_human",
    "completed_no_action",
    "schema_failed",        # model output broke the schema — terminal by design
    "budget_exceeded",      # retrying with the same budget cannot help
    "pending_approval",     # paused at the interrupt; humans take it from here
    "refund_not_approved",  # human said no on resume; no money moved, verdict recorded
})

# Failure statuses whose error TEXT decides retryability.
_AMBIGUOUS_STATUSES = frozenset({
    "evidence_failed", "refund_request_failed", "refund_execution_failed",
})

# Markers that prove a transient world (checked case-insensitively).
_TRANSIENT_MARKERS = (
    "GATEWAY_UNREACHABLE", "RATE_LIMITED", "RATE LIMIT", "TIMEOUT",
    "TIMED OUT", "SERVER_ERROR", "SERVICE_UNAVAILABLE",
    "INTERNAL_SERVER_ERROR", "CONNECTION", "503", "502", "500", "429",
)

_MAX_MESSAGE = 500


def classify_run(status: str, text: str = "") -> str:
    """'processed' | 'retryable' | 'permanent' for a finished run.

    - verdict statuses          -> processed
    - rejected_by_critic        -> retryable (the draft is one sample;
                                   resampling re-runs every money gate)
    - llm_error                 -> retryable (provider hiccup AFTER the
                                   client's own backoff ladder gave up)
    - ambiguous failure statuses-> retryable only if the error text carries
                                   a transient marker; a definite failure
                                   (RESOURCE_NOT_FOUND, policy rejections)
                                   is permanent — retrying cannot change it
    - anything unrecognized     -> processed (the run completed; never loop
                                   on a vocabulary we don't know)
    """
    if status in _PROCESSED_STATUSES:
        return "processed"
    if status == "rejected_by_critic":
        # The draft is ONE sample; every money gate (critic, decide, policy,
        # approvals) re-evaluates on resample, so retrying weakens nothing.
        # A phrasing flake must not kill a job: live 2026-11-10 flash-lite
        # re-phrased a packet fact as "we have no record of delivery" and
        # the critic (correctly) rejected that single sample.
        return "retryable"
    if status == "llm_error":
        return "retryable"
    if status in _AMBIGUOUS_STATUSES:
        upper = (text or "").upper()
        if any(marker in upper for marker in _TRANSIENT_MARKERS):
            return "retryable"
        return "permanent"
    return "processed"


class GatewayAudit:
    """Writes the 'agent_run_finished' audit row into the GATEWAY database
    (the worker's engine points there — the queue lives in the same DB).

    The agent id is resolved once from the agent's API key (the worker runs
    as exactly one identity). If the key is unknown the rows are still
    written with agent_id NULL — audit_log allows it, and an unaudited
    outcome is worse than an anonymously-audited one.
    """

    def __init__(self, engine: Engine, api_key: str):
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._agent_id: int | None = None
        if api_key:
            with self._sessions() as s:
                self._agent_id = s.execute(
                    select(AgentKey.agent_id)
                    .where(AgentKey.key_hash == hash_api_key(api_key),
                           AgentKey.revoked_at.is_(None))
                ).scalar_one_or_none()
            if self._agent_id is None:
                logger.warning("AGENT_API_KEY does not match any active key — "
                               "audit rows will carry agent_id=NULL")

    def __call__(self, outcome: dict) -> None:
        detail = {
            "run_id": outcome.get("run_id"),
            "dispute_id": outcome.get("dispute_id"),
            "summary": (outcome.get("summary") or "")[:500],
            "usage": outcome.get("usage") or {},
            "already_completed": bool(outcome.get("already_completed")),
        }
        interrupt = outcome.get("interrupt")
        if interrupt:
            detail["approval_id"] = interrupt.get("approval_id")
        with self._sessions() as s:
            record(s, decision="agent_run_finished",
                   reason_code=outcome.get("status", "unknown"),
                   agent_id=self._agent_id, detail=detail)
            s.commit()  # commit before you speak


class Dispatcher:
    """The worker's handler. job_type -> action:
    - dispute_agent: run the dispute agent for payload['dispute_id'].
    - resume_agent: resume the run paused at payload['approval_id'] with
      the human's decision (payload['approved']).
    - anything else: PermanentJobError (dead-letters visibly instead of
      silently piling up).
    """

    def __init__(self, run_dispute: Callable[[str], dict],
                 resume: Callable[[int, bool, str], dict] | None = None,
                 audit: Callable[[dict], None] | None = None):
        self._run_dispute = run_dispute
        self._resume = resume
        self._audit = audit or (lambda outcome: None)

    def __call__(self, job_type: str, payload: dict) -> None:
        if job_type == "dispute_agent":
            return self._handle_dispute(payload or {})
        if job_type == "resume_agent":
            if self._resume is None:
                raise PermanentJobError(
                    "RESUME_NOT_CONFIGURED: worker has no resume handler")
            return self._handle_resume(payload or {})
        raise PermanentJobError(f"UNKNOWN_JOB_TYPE: {job_type!r}")

    def _handle_dispute(self, payload: dict) -> None:
        dispute_id = payload.get("dispute_id")
        if not isinstance(dispute_id, str) or not dispute_id.strip():
            raise PermanentJobError(
                f"MALFORMED_PAYLOAD: missing dispute_id in {str(payload)[:200]}")

        outcome = self._run_dispute(dispute_id)  # may raise: worker classifies
        self._settle(outcome)
        logger.info("Dispute %s: run %s finished with %s",
                    dispute_id, outcome.get("run_id"),
                    outcome.get("status"))

    def _handle_resume(self, payload: dict) -> None:
        approval_id = payload.get("approval_id")
        approved = payload.get("approved")
        # bool is an int subclass — reject True/False masquerading as an id.
        if not isinstance(approval_id, int) or isinstance(approval_id, bool):
            raise PermanentJobError(
                f"MALFORMED_PAYLOAD: resume job needs an integer "
                f"approval_id, got {approval_id!r}")
        if not isinstance(approved, bool):
            raise PermanentJobError(
                f"MALFORMED_PAYLOAD: resume job needs a boolean 'approved', "
                f"got {approved!r}")
        decided_by = str(payload.get("decided_by") or "unknown")

        outcome = self._resume(approval_id, approved, decided_by)
        self._settle(outcome)
        logger.info("Approval %d (%s): run %s finished with %s",
                    approval_id, "approved" if approved else "rejected",
                    outcome.get("run_id"), outcome.get("status"))

    def _settle(self, outcome: dict) -> None:
        """Audit the outcome BEFORE finalizing, then translate the run's
        terminal status into the job's fate. An audit-write failure
        propagates: the job retries, the runner's pre-checks make the
        re-run cheap, and the audit row lands on the next attempt."""
        self._audit(outcome)
        status = outcome.get("status", "unknown")
        verdict = classify_run(status, outcome.get("summary") or "")
        if verdict == "retryable":
            raise RetryableJobError(
                f"{status}: {(outcome.get('summary') or '')[:_MAX_MESSAGE]}")
        if verdict == "permanent":
            raise PermanentJobError(
                f"{status}: {(outcome.get('summary') or '')[:_MAX_MESSAGE]}")
