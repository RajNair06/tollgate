"""Runs the dispute agent for one webhook job.

Identity — the duplicate-run guard, by construction:
- run_id = f"webhook-{dispute_id}" — DETERMINISTIC. The checkpoint
  thread_id, the agent_runs primary key, and the refund idempotency key
  (f"{run_id}-refund", derived inside the graph) all inherit that
  determinism.
- Before invoking, the thread's checkpoint is inspected:
  * terminal status already recorded  -> NOT re-run; the recorded outcome
    is returned (at-least-once dispatch + this check = one run per dispute)
  * paused at the approval interrupt  -> reported as pending_approval
    again, without disturbing the pause
  * crashed mid-run (non-terminal)    -> invoke continues the checkpoint;
    a re-requested refund replays with the SAME idempotency key, so the
    gateway dedups instead of double-refunding.

The outbox's dedup_key already prevents duplicate JOBS per dispute; this
layer prevents duplicate RUNS even when the same dispute arrives through
a path that bypassed that guard (manual re-enqueue, a reclaimed lease).

Pausing and resuming:
- At an approval interrupt the run writes a PROVISIONAL agent_runs row
  (status pending_approval, approval_id set) — finalize never ran, and
  without the row neither resume lookup nor the dashboard could see the
  paused run.
- resume_approval(approval_id, approved, decided_by) finds the run via
  agent_runs.approval_id, re-checks the checkpoint (still paused? same
  approval id?), and re-enters the graph with Command(resume=...).
  Mismatched or unknown approvals are PermanentJobErrors — a wrong id
  must never move money.
"""

from __future__ import annotations

import logging
from typing import Callable

from langgraph.types import Command

from agent.budget import RunMeter
from agent.checkpoint import ensure_database, open_checkpointer
from agent.config import AgentSettings
from agent.gemini_client import GeminiClient
from agent.graph import build_graph
from agent.runs import RunLog
from agent.tools import build_tollgate_registry
from worker.errors import PermanentJobError

logger = logging.getLogger(__name__)

RUNNING = "running"


def run_id_for(dispute_id: str) -> str:
    """The one run identity for a dispute — thread, run log, and refund
    idempotency key all derive from it."""
    return f"webhook-{dispute_id}"


def _interrupt_payload(snapshot) -> dict | None:
    """Pending interrupt payload from a graph state snapshot (or None)."""
    for task in getattr(snapshot, "tasks", ()):
        for intr in getattr(task, "interrupts", ()):
            return dict(intr.value)
    return None


class AgentRunner:
    """Owns the agent-side long-lived resources: the checkpointer
    connection and the run log (its own PostgreSQL database, separate
    from the gateway's). Graphs are built per run — cheap, and each gets
    a fresh RunMeter with the deterministic run id.

    graph_builder is injectable for tests: production builds a GeminiClient
    + MCP registry; tests build the real compiled graph over ScriptedLLM /
    FakeRegistry (agent.testing).
    """

    def __init__(self, settings: AgentSettings,
                 graph_builder: Callable[[str, "AgentRunner", dict | None],
                                         object] | None = None):
        self._settings = settings
        ensure_database(settings.database_url)
        self._conn, self._saver = open_checkpointer(settings.database_url)
        self._run_log = RunLog(settings.database_url)
        self._run_log.init()
        self._graph_builder = graph_builder or _build_production_graph

    @property
    def saver(self):
        return self._saver

    @property
    def run_log(self) -> RunLog:
        return self._run_log

    def close(self) -> None:
        self._conn.close()
        self._run_log.dispose()

    def run_dispute(self, dispute_id: str) -> dict:
        """Run (or recover) the agent for one dispute. Returns the outcome
        dict the dispatcher audits and classifies:
        {run_id, dispute_id, status, summary, usage, interrupt?,
         already_completed?}
        """
        run_id = run_id_for(dispute_id)
        graph = self._graph_builder(run_id, self, None)
        config = {"configurable": {"thread_id": run_id}}

        snapshot = graph.get_state(config)
        if snapshot.values:
            paused = _interrupt_payload(snapshot)
            if paused is not None:
                logger.info("Run %s already paused at approval #%s — not disturbing it",
                            run_id, paused.get("approval_id"))
                # Re-write the pause row: if the previous attempt crashed
                # between invoke and this write, resume lookup would be
                # blind without it. Upsert — harmless when already present.
                self._record_pause(run_id, dispute_id, paused, snapshot.values)
                return {"run_id": run_id, "dispute_id": dispute_id,
                        "status": "pending_approval", "interrupt": paused,
                        "summary": f"Awaiting human approval #{paused.get('approval_id')}",
                        "usage": snapshot.values.get("usage") or {},
                        "already_completed": True}
            status = snapshot.values.get("status")
            if status and status != RUNNING:
                logger.info("Run %s already finished (%s) — not re-running", run_id, status)
                return {"run_id": run_id, "dispute_id": dispute_id,
                        "status": status,
                        "summary": snapshot.values.get("summary") or status,
                        "usage": snapshot.values.get("usage") or {},
                        "already_completed": True}

        result = graph.invoke(
            {"run_id": run_id, "dispute_id": dispute_id, "status": RUNNING},
            config)

        if "__interrupt__" in result:
            payload = dict(result["__interrupt__"][0].value)
            self._record_pause(run_id, dispute_id, payload, result)
            return {"run_id": run_id, "dispute_id": dispute_id,
                    "status": "pending_approval", "interrupt": payload,
                    "summary": f"Awaiting human approval #{payload.get('approval_id')}",
                    "usage": result.get("usage") or {}}
        return {"run_id": run_id, "dispute_id": dispute_id,
                "status": result.get("status", "unknown"),
                "summary": result.get("summary") or result.get("error") or "",
                "usage": result.get("usage") or {}}

    def resume_approval(self, approval_id: int, approved: bool,
                        decided_by: str = "unknown") -> dict:
        """Resume the run paused at approval `approval_id`.

        The resume job knows only the approval — the run is found through
        agent_runs.approval_id (written at pause time). Safety rails:
        - no run on record for the approval  -> PermanentJobError
        - checkpoint gone                    -> PermanentJobError
        - paused for a DIFFERENT approval id -> PermanentJobError (an
          operator deciding the wrong approval must not move money on
          this run)
        - already resumed and terminal       -> recorded outcome returned
          (a redelivered resume job is a no-op, money moved once)

        On resume, LangGraph re-runs execute_refund from the top; the
        refund REQUEST is not repeated (it lives in the completed
        request_refund node) and the gateway's approval-transition guard
        plus the deterministic idempotency key make replays dedup.
        """
        row = self._run_log.find_by_approval(approval_id)
        if row is None:
            raise PermanentJobError(
                f"NO_RUN_FOR_APPROVAL: approval {approval_id} has no agent "
                f"run on record")
        run_id = row["run_id"]
        dispute_id = row.get("dispute_id") or ""

        graph = self._graph_builder(run_id, self, None)
        config = {"configurable": {"thread_id": run_id}}
        snapshot = graph.get_state(config)
        if not snapshot.values:
            raise PermanentJobError(
                f"NO_CHECKPOINT: run {run_id} has no stored state")

        paused = _interrupt_payload(snapshot)
        if paused is None:
            status = snapshot.values.get("status")
            if status and status != RUNNING:
                logger.info("Run %s already resumed (status %s) — "
                            "reporting the recorded outcome", run_id, status)
                return {"run_id": run_id, "dispute_id": dispute_id,
                        "status": status,
                        "summary": snapshot.values.get("summary") or status,
                        "usage": snapshot.values.get("usage") or {},
                        "already_completed": True}
            raise PermanentJobError(
                f"NOT_PAUSED: run {run_id} (status={status!r}) is not "
                f"waiting at an interrupt")
        if paused.get("approval_id") != approval_id:
            raise PermanentJobError(
                f"APPROVAL_MISMATCH: run {run_id} is paused for approval "
                f"{paused.get('approval_id')}, resume requested {approval_id}")

        # Rebuild with the prior usage seeded into the fresh meter so the
        # finalized run log shows the FULL run cost, not the resume's share.
        graph = self._graph_builder(run_id, self,
                                    snapshot.values.get("usage") or None)
        result = graph.invoke(Command(resume={
            "approved": bool(approved),
            "approval_id": approval_id,
            "decided_by": decided_by,
        }), config)

        if "__interrupt__" in result:  # defensive: a resume should not re-pause
            payload = dict(result["__interrupt__"][0].value)
            self._record_pause(run_id, dispute_id, payload, result)
            return {"run_id": run_id, "dispute_id": dispute_id,
                    "status": "pending_approval", "interrupt": payload,
                    "summary": f"Still awaiting approval #{payload.get('approval_id')}",
                    "usage": result.get("usage") or {}}
        return {"run_id": run_id, "dispute_id": dispute_id,
                "status": result.get("status", "unknown"),
                "summary": result.get("summary") or result.get("error") or "",
                "usage": result.get("usage") or {}}

    def _record_pause(self, run_id: str, dispute_id: str,
                      payload: dict, values: dict) -> None:
        """Provisional agent_runs row written AT PAUSE time.

        finalize() only runs when the graph reaches a terminal node — a
        paused run would otherwise be invisible to both the resume lookup
        and the dashboard. A failure here must fail the job: without the
        row, a later resume_agent job could never find this run. (The
        retry re-attempts the write; the run itself is protected by the
        checkpoint pre-check.)
        """
        decision = (values or {}).get("decision") or {}
        self._run_log.write({
            "run_id": run_id,
            "dispute_id": dispute_id,
            "status": "pending_approval",
            "summary": f"Awaiting human approval #{payload.get('approval_id')}",
            "usage": (values or {}).get("usage") or {},
            "confidence": decision.get("confidence"),
            "proposed_action": decision.get("proposed_action"),
            "approval_id": payload.get("approval_id"),
        })


def _seed_meter(meter: RunMeter, prior_usage: dict | None) -> None:
    """Carry pre-interrupt usage into a resumed run's meter so the run log
    shows the FULL run cost, not just the resume's share (same as the CLI)."""
    if not prior_usage:
        return
    meter.usage.input_tokens = prior_usage.get("input_tokens", 0)
    meter.usage.output_tokens = prior_usage.get("output_tokens", 0)
    meter.llm_calls = prior_usage.get("llm_calls", 0)


def _build_production_graph(run_id: str, runner: AgentRunner,
                            prior_usage: dict | None = None):
    """The real wiring: Gemini client with a fresh meter bound to run_id,
    MCP tool registry, PostgresSaver checkpointer, run-log writer."""
    s = runner._settings
    meter = RunMeter(model=s.model, budget_tokens=s.token_budget)
    meter.run_id = run_id
    _seed_meter(meter, prior_usage)
    llm = GeminiClient(s.gemini_api_key, s.model, meter=meter,
                       max_retries=s.max_retries,
                       backoff_base=s.backoff_base_seconds,
                       backoff_cap=s.backoff_max_seconds)
    registry = build_tollgate_registry(s.mcp_url, s.gateway_api_key)
    return build_graph(llm, registry, prompt_version=s.prompt_version,
                       checkpointer=runner.saver,
                       run_log=runner.run_log.write,
                       min_confidence=s.min_confidence)
