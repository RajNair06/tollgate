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
"""

from __future__ import annotations

import logging
from typing import Callable

from agent.budget import RunMeter
from agent.checkpoint import ensure_database, open_checkpointer
from agent.config import AgentSettings
from agent.gemini_client import GeminiClient
from agent.graph import build_graph
from agent.runs import RunLog
from agent.tools import build_tollgate_registry

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
                 graph_builder: Callable[[str, "AgentRunner"], object] | None = None):
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
        graph = self._graph_builder(run_id, self)
        config = {"configurable": {"thread_id": run_id}}

        snapshot = graph.get_state(config)
        if snapshot.values:
            paused = _interrupt_payload(snapshot)
            if paused is not None:
                logger.info("Run %s already paused at approval #%s — not disturbing it",
                            run_id, paused.get("approval_id"))
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
            return {"run_id": run_id, "dispute_id": dispute_id,
                    "status": "pending_approval", "interrupt": payload,
                    "summary": f"Awaiting human approval #{payload.get('approval_id')}",
                    "usage": result.get("usage") or {}}
        return {"run_id": run_id, "dispute_id": dispute_id,
                "status": result.get("status", "unknown"),
                "summary": result.get("summary") or result.get("error") or "",
                "usage": result.get("usage") or {}}


def _build_production_graph(run_id: str, runner: AgentRunner):
    """The real wiring: Gemini client with a fresh meter bound to run_id,
    MCP tool registry, PostgresSaver checkpointer, run-log writer."""
    s = runner._settings
    meter = RunMeter(model=s.model, budget_tokens=s.token_budget)
    meter.run_id = run_id
    llm = GeminiClient(s.gemini_api_key, s.model, meter=meter,
                       max_retries=s.max_retries,
                       backoff_base=s.backoff_base_seconds,
                       backoff_cap=s.backoff_max_seconds)
    registry = build_tollgate_registry(s.mcp_url, s.gateway_api_key)
    return build_graph(llm, registry, prompt_version=s.prompt_version,
                       checkpointer=runner.saver,
                       run_log=runner.run_log.write,
                       min_confidence=s.min_confidence)
