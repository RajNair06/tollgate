"""python -m worker — run the outbox worker process.

Wiring: WorkerSettings (queue DB) + AgentSettings (MCP url, agent key,
Gemini key, agent DB) -> AgentRunner -> Dispatcher(+GatewayAudit) -> Worker.

The worker holds NO PayPal credentials: money moves only through the
gateway's MCP tools, exactly like any other agent.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from dotenv import load_dotenv
from sqlalchemy import create_engine

load_dotenv()

from agent.config import get_agent_settings  # noqa: E402
from worker.agent_runner import AgentRunner  # noqa: E402
from worker.config import get_worker_settings  # noqa: E402
from worker.dispatch import Dispatcher, GatewayAudit  # noqa: E402
from worker.worker import Worker  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("worker")


def main() -> int:
    agent_settings = get_agent_settings()
    if not agent_settings.gateway_api_key:
        print("AGENT_API_KEY must be set (the webhook agent's toll-gate key)",
              file=sys.stderr)
        return 2
    if not agent_settings.gemini_api_key:
        print("GEMINI_API_KEY must be set", file=sys.stderr)
        return 2

    worker_settings = get_worker_settings()
    runner = AgentRunner(agent_settings)
    # The audit sink writes to the SAME gateway DB the worker's engine owns.
    engine = create_engine(worker_settings.database_url, pool_size=2,
                           max_overflow=3, pool_pre_ping=True)
    audit = GatewayAudit(engine, agent_settings.gateway_api_key)
    worker = Worker(engine, Dispatcher(runner.run_dispute, audit=audit),
                    worker_settings)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    logger.info("Outbox worker starting (queue: %s, agent MCP: %s)",
                worker_settings.database_url, agent_settings.mcp_url)
    try:
        worker.run_forever(stop)
    finally:
        runner.close()
        worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
