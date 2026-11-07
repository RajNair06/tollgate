"""CLI: run (or resume) one dispute-handling agent run.

Fresh run:
    uv run python -m agent.run_dispute PP-D-56244

The run pauses with an interrupt when the gateway demands human approval.
The paused state lives in PostgreSQL (checkpointer), NOT in this process —
the machine can reboot. After a human decides the approval through the
gateway API, resume with:

    uv run python -m agent.run_dispute --resume <run_id> --approved
    uv run python -m agent.run_dispute --resume <run_id> --rejected

The resume reads the interrupt payload from the checkpoint to recover the
approval_id, re-enters the graph on the same thread, and the execute_refund
node completes the run.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from agent.budget import RunMeter                     # noqa: E402
from agent.checkpoint import ensure_database, open_checkpointer  # noqa: E402
from agent.config import get_agent_settings            # noqa: E402
from agent.gemini_client import GeminiClient           # noqa: E402
from agent.graph import build_graph                    # noqa: E402
from agent.runs import RunLog                          # noqa: E402
from agent.tools import build_tollgate_registry        # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("agent.run_dispute")

_FAILURE_STATUSES = {
    "evidence_failed", "budget_exceeded", "llm_error", "schema_failed",
    "refund_request_failed", "refund_execution_failed",
}


def _interrupt_payload(state) -> dict | None:
    """Extract the pending interrupt payload from a paused graph state."""
    for task in getattr(state, "tasks", ()):
        for intr in getattr(task, "interrupts", ()):
            return dict(intr.value)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dispute_id", nargs="?", help="e.g. PP-D-56244 (fresh run)")
    parser.add_argument("--resume", metavar="RUN_ID",
                        help="resume a paused (interrupted) run by its run_id")
    parser.add_argument("--approved", action="store_true",
                        help="on resume: the human approved the refund")
    parser.add_argument("--rejected", action="store_true",
                        help="on resume: the human did not approve")
    args = parser.parse_args(argv)

    settings = get_agent_settings()
    if not settings.gateway_api_key:
        print("AGENT_API_KEY must be set (the agent's toll-gate key)", file=sys.stderr)
        return 2
    if not settings.gemini_api_key:
        print("GEMINI_API_KEY must be set", file=sys.stderr)
        return 2
    if not args.resume and not args.dispute_id:
        parser.error("provide a dispute_id for a fresh run, or --resume RUN_ID")
    if args.approved and args.rejected:
        parser.error("--approved and --rejected are mutually exclusive")

    ensure_database(settings.database_url)
    conn, saver = open_checkpointer(settings.database_url)
    run_log = RunLog(settings.database_url)
    run_log.init()

    meter = RunMeter(model=settings.model, budget_tokens=settings.token_budget)
    llm = GeminiClient(settings.gemini_api_key, settings.model, meter=meter,
                       max_retries=settings.max_retries,
                       backoff_base=settings.backoff_base_seconds,
                       backoff_cap=settings.backoff_max_seconds)
    registry = build_tollgate_registry(settings.mcp_url, settings.gateway_api_key)
    graph = build_graph(llm, registry, prompt_version=settings.prompt_version,
                        checkpointer=saver, run_log=run_log.write,
                        min_confidence=settings.min_confidence)

    try:
        if args.resume:
            run_id = args.resume
            meter.run_id = run_id
            config = {"configurable": {"thread_id": run_id}}
            state = graph.get_state(config)
            if not state.values:
                print(f"no checkpoint found for run {run_id}", file=sys.stderr)
                return 2
            payload = _interrupt_payload(state)
            if payload is None:
                print(f"run {run_id} is not paused at an interrupt "
                      f"(status: {state.values.get('status')})", file=sys.stderr)
                return 2
            # Carry pre-interrupt token usage into the resumed meter so the
            # run log shows the FULL run cost, not just the resume's share.
            prior = state.values.get("usage") or {}
            meter.usage.input_tokens = prior.get("input_tokens", 0)
            meter.usage.output_tokens = prior.get("output_tokens", 0)
            meter.llm_calls = prior.get("llm_calls", 0)

            from langgraph.types import Command
            result = graph.invoke(Command(resume={
                "approved": bool(args.approved),
                "approval_id": payload.get("approval_id"),
                "decided_by": "cli-operator",
            }), config)
        else:
            run_id = meter.run_id
            config = {"configurable": {"thread_id": run_id}}
            result = graph.invoke(
                {"run_id": run_id, "dispute_id": args.dispute_id,
                 "status": "running"},
                config)

        if "__interrupt__" in result:
            payload = result["__interrupt__"][0].value
            print(json.dumps({
                "run_id": run_id,
                "paused": "approval_required",
                "interrupt": payload,
                "resume_with": (
                    f"uv run python -m agent.run_dispute --resume {run_id} "
                    f"--approved   (or --rejected)"),
            }, indent=2))
            return 0

        status = result.get("status", "unknown")
        print(json.dumps({
            "run_id": run_id,
            "status": status,
            "summary": result.get("summary"),
            "decision": result.get("decision"),
            "usage": result.get("usage"),
        }, indent=2, default=str))
        return 1 if status in _FAILURE_STATUSES else 0
    finally:
        conn.close()
        run_log.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
