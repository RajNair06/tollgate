"""Optional IN-PROCESS outbox worker — the free-tier accommodation.

Why this exists: Render's free plan cannot run background workers, so a
deployment pinned to the free plan sets ``WORKER_IN_PROCESS=1`` and this
module runs the exact same Worker stack ``python -m worker`` runs
(WorkerSettings + AgentSettings -> AgentRunner -> Dispatcher(+GatewayAudit)
-> Worker) as a daemon thread inside the FastAPI process.

The SEPARATE process remains the recommended shape — the README's
Deployment section says plainly that this mode exists only to stay on
Render's free plan. What the tradeoff costs:

- On a free host that sleeps when idle, the worker sleeps with it. Queued
  jobs wait for the next wake-up (a webhook delivery or dashboard traffic
  wakes the service; PayPal retries until it gets a 200). Correctness is
  unaffected — claims are SKIP LOCKED, crashed leases are swept, dispatch
  is idempotent — only latency degrades.
- Keep exactly ONE replica (the free plan gives you one anyway). Even
  multiple replicas would stay correct (SKIP LOCKED), just wasteful.

Deliberate behavior: missing AGENT_API_KEY / GEMINI_API_KEY logs loudly
and declines to start the thread INSTEAD of crashing the web service. The
key is provisioned through the admin API of the deployed gateway, so
crashing here would deadlock the very first deploy (chicken and egg).
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger("worker.inprocess")

_TRUTHY = {"1", "true", "yes", "on"}


def worker_requested() -> bool:
    """True when WORKER_IN_PROCESS asks for the embedded worker."""
    return os.environ.get("WORKER_IN_PROCESS", "").strip().lower() in _TRUTHY


def start_in_process_worker() -> tuple[threading.Thread, threading.Event] | None:
    """Build and start the worker thread.

    Returns ``(thread, stop_event)`` when started, or ``None`` when the
    worker declined to start (missing credentials — logged as an error).
    The caller owns the stop event: setting it ends the poll loop, and
    the thread (daemon) disposes its engine and runner on the way out.
    """
    # Imported lazily: a gateway that does not embed the worker should
    # not pay for (or be broken by) these imports.
    from sqlalchemy import create_engine

    from agent.config import get_agent_settings
    from worker.agent_runner import AgentRunner
    from worker.config import get_worker_settings
    from worker.dispatch import Dispatcher, GatewayAudit
    from worker.worker import Worker

    agent_settings = get_agent_settings()
    if not agent_settings.gateway_api_key:
        logger.error(
            "WORKER_IN_PROCESS is set but AGENT_API_KEY is missing — the "
            "in-process worker is NOT running. Webhook jobs will queue "
            "unprocessed until a key is provisioned (POST /v1/admin/agents) "
            "and the service restarts.")
        return None
    if not agent_settings.gemini_api_key:
        logger.error(
            "WORKER_IN_PROCESS is set but GEMINI_API_KEY is missing — the "
            "in-process worker is NOT running.")
        return None

    worker_settings = get_worker_settings()
    runner = AgentRunner(agent_settings)
    engine = create_engine(worker_settings.database_url, pool_size=2,
                           max_overflow=3, pool_pre_ping=True)
    audit = GatewayAudit(engine, agent_settings.gateway_api_key)
    worker = Worker(engine,
                    Dispatcher(runner.run_dispute,
                               resume=runner.resume_approval,
                               audit=audit),
                    worker_settings)
    stop = threading.Event()

    def _run() -> None:
        try:
            worker.run_forever(stop)
        finally:
            runner.close()
            engine.dispose()

    thread = threading.Thread(target=_run, name="in-process-worker",
                              daemon=True)
    thread.start()
    logger.info(
        "In-process outbox worker started (free-plan accommodation — the "
        "recommended shape is the separate `python -m worker` process)")
    return thread, stop
