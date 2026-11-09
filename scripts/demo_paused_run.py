"""Seed a PAUSED (pending-approval) agent run into the DEV databases.

Why this script exists: the PayPal sandbox has no real disputes, so a
full webhook->agent run cannot pause on a real refund. This script
bridges the gap for demoing the dashboard's approve->resume->refund
loop:

  1. You first create a REAL pending approval through the live gateway
     (POST /v1/agent-actions with an amount >= the agent's approval
     threshold and reference_id = a real sandbox capture).
  2. This script then runs the REAL compiled LangGraph against a
     SCRIPTED world (fake dispute, canned LLM, fake tool registry whose
     issue_refund response carries YOUR real approval id) with the
     REAL AgentRunner — so the checkpoint (dev agent DB) and the
     provisional agent_runs row (status pending_approval,
     approval_id set, worst-case reasoning text) are exactly what the
     production worker writes at a pause.
  3. Approve it from the dashboard: the REAL worker picks up the
     resume_agent job, finds this run by approval_id, resumes the
     checkpoint, and executes the refund through the REAL gateway —
     real sandbox money moves, end to end.

The reasoning/evidence text is deliberately worst-case: ~1000 chars,
embedded newlines, quoted punctuation, and a 200-char unbroken token — the
dashboard must wrap all of it inside the cell, never clip.

Usage:
    uv run python scripts/demo_paused_run.py --approval-id 5 --action-id 66
    (env: AGENT_DATABASE_URL, defaults to the dev agent DB)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.budget import RunMeter
from agent.config import AgentSettings
from agent.graph import build_graph
from agent.testing import (
    CAPTURE_ID,
    CLEAN_DRAFT,
    CRITIC_OK,
    DISPUTE,
    FakeRegistry,
    ScriptedLLM,
    envelope_dispute,
    envelope_refund_pending,
    envelope_transaction,
)
from worker.agent_runner import AgentRunner

LONG_TOKEN = (
    "TXN-REF-8f3a9c2e7b41d6059a8e7f31c2b49d8e0f7a61b5c3d92e8470f1a6b5c4d3e2"
    "f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a1b2c3d4e5"
    "f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1"
)

WORST_CASE_REASONING = (
    "Item-not-received dispute with NO delivery evidence anywhere in the packet.\n\n"
    "Findings:\n"
    "1. The capture completed on 2026-10-28 for the full order amount, and the "
    "merchant's tracking feed shows the label was created but the parcel was never "
    "scanned into the carrier network — 30 days of total silence.\n"
    "2. The buyer contacted the merchant twice (2026-10-12 and 2026-10-20) and "
    "received only an automated reply; the merchant never provided a delivery "
    "confirmation, a signature, or a carrier case number.\n"
    "3. The dispute reason code MERCHANDISE_OR_SERVICE_NOT_RECEIVED matches the "
    "evidence exactly, and the requested amount is within the capture's remaining "
    "refundable balance.\n\n"
    "Counter-evidence considered and rejected: the merchant flagged the buyer as "
    "\u201chigh-risk\u201d but supplied no prior chargeback history, "
    "no AVS mismatch, and no delivery proof — under policy §4.2 an unsupported "
    "risk flag does not outweigh a clean item-not-received packet.\n\n"
    "Long-reference stress token (must wrap, never clip): " + LONG_TOKEN + "\n\n"
    "Conclusion: refund the disputed amount; confidence is high because every "
    "cited fact comes straight from the transaction and dispute envelopes."
)


def worst_case_decision(dispute_id: str) -> str:
    return json.dumps({
        "dispute_id": dispute_id,
        "proposed_action": "issue_refund",
        "refund_amount": "3.00",
        "currency": "USD",
        "confidence": 0.83,
        "reasoning": WORST_CASE_REASONING,
        "transaction_id": CAPTURE_ID,
        "cited_evidence": [
            {
                "source": "transaction",
                "claim": "capture COMPLETED for the order amount; remaining "
                         "refundable balance covers the requested refund",
                "value": "10.99",
                "transaction_id": CAPTURE_ID,
                "observed_at": "2026-10-28T09:00:00Z",
            },
            {
                "source": "dispute",
                "claim": "reason code on record matches an item-not-received "
                         "packet with no delivery evidence",
                "value": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
            },
            {
                "source": "dispute",
                "claim": "buyer's own words (quotes, dashes, dates — punctuation stress)",
                "value": "\u201cPackage never arrived \u2014 30 days, no tracking movement "
                         "whatsoever. Emailed twice (2026-10-12, 2026-10-20), got "
                         "only an automated reply.\u201d",
            },
        ],
    })


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--approval-id", type=int, required=True,
                   help="the REAL pending approval id from the live gateway")
    p.add_argument("--action-id", type=int, required=True,
                   help="the REAL action id backing that approval")
    p.add_argument("--dispute-id", default="PP-D-DEMO-9001",
                   help="fake dispute id for the scripted evidence world")
    args = p.parse_args(argv)

    db = os.environ.get(
        "AGENT_DATABASE_URL",
        "postgresql://tollgate:tollgate@localhost:5433/agent")
    settings = AgentSettings(
        mcp_url="http://unused.invalid/mcp",   # the paused half makes no
        gateway_api_key="tg_unused_for_pause",  # live tool calls — resume
        gemini_api_key="dummy",                 # (by the real worker) does
        model="scripted",
        token_budget=100_000,
        database_url=db,
        prompt_version="v1",
        min_confidence=0.6,
    )

    dispute = dict(DISPUTE)
    dispute["dispute_id"] = args.dispute_id
    responses = {
        "get_dispute": envelope_dispute(dispute),
        "get_transaction": envelope_transaction(),
        # The pause payload carries the REAL approval id — that link is what
        # the dashboard's approve button and the worker's resume job use.
        "issue_refund": envelope_refund_pending(approval_id=args.approval_id,
                                                action_id=args.action_id),
    }
    script = [CLEAN_DRAFT, CRITIC_OK, worst_case_decision(args.dispute_id)]

    def builder(run_id, runner, prior_usage=None):
        meter = RunMeter(model="scripted", budget_tokens=100_000)
        meter.run_id = run_id
        llm = ScriptedLLM(script, meter=meter)
        return build_graph(llm, FakeRegistry(dict(responses)),
                           prompt_version="v1",
                           checkpointer=runner.saver,
                           run_log=runner.run_log.write,
                           min_confidence=0.6)

    runner = AgentRunner(settings, graph_builder=builder)
    try:
        outcome = runner.run_dispute(args.dispute_id)
    finally:
        runner.close()

    print(json.dumps({k: v for k, v in outcome.items() if k != "interrupt"},
                     indent=2, default=str))
    if outcome.get("status") != "pending_approval":
        print("ERROR: run did not pause — check the scripted world",
              file=sys.stderr)
        return 1
    print(f"\nPaused. run_id={outcome['run_id']} "
          f"approval_id={args.approval_id}")
    print("Approve it from the dashboard — the worker will resume this run "
          "and execute the real sandbox refund.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
