"""The LangGraph StateGraph: dispute in → verified decision (+ maybe refund).

Topology (conditional edges in CAPS):

    START → gather_evidence →[evidence ok?]→ draft_response → critic
                                └──────────(fail)──────────────────────┐
    critic →[claims supported?]→ decide_refund                         │
                └──(violations)──────────────────────────────┐         │
    decide_refund →[action == issue_refund?]→ request_refund │         │
                        └──(deny/escalate/invalid)─────┐     │         │
    request_refund →[pending_approval?]→ execute_refund│     │         │
                        └──(executed/denied)─────┐     │     │         │
    execute_refund ──(interrupt ⇄ resume)──┐     │     │     │         │
                                           ▼     ▼     ▼     ▼         ▼
                                         finalize → END

Deliberate choices:
- ONE-PASS critic, no revision loop: a draft with unsupported claims is not
  sent back for a rewrite — the run terminates as rejected_by_critic. A
  revision loop lets the model launder a hallucination by rewording it;
  termination keeps the failure visible and cheap.
- The critic is BOTH an LLM verdict and a deterministic scan: every
  17-char-ID-shaped and timestamp-shaped token in the draft must appear
  verbatim in the evidence JSON. The deterministic half cannot be talked
  out of its verdict.
- Refund request and refund execution are SEPARATE nodes. LangGraph resumes
  an interrupted node by RE-RUNNING it from its first line — if the PayPal
  call and the interrupt lived in one node, the resume would re-issue the
  refund request (safe, deduplicated — but its envelope would no longer
  carry the approval_id). Splitting them means the resume re-runs only
  execute_refund, whose first act after the interrupt is executing the
  already-approved refund.
- Every LLM call is wrapped: budget exhaustion or an LLM failure becomes a
  terminal status routed to finalize — the run always ends with a recorded
  status and a run-log row, never with an unhandled exception.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent.budget import LLMBudgetExceeded, RunMeter
from agent.gemini_client import LLMClient, LLMError
from agent.prompts import load_prompt
from agent.state import MIN_CONFIDENCE_FOR_ACTION, AgentState, RefundDecision
from agent.tools import ToolRegistry, ToolCallError, interpret_refund_result

logger = logging.getLogger(__name__)

# Deterministic hallucination scan: anything shaped like a PayPal ID or an
# RFC-3339 timestamp in the draft must exist verbatim in the evidence.
_ID_RE = re.compile(r"\b[A-Z0-9]{17}\b")
_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\b")

RUNNING = "running"


def _fail(status: str, error: str) -> dict:
    return {"status": status, "error": error}


def _safe_generate(llm: LLMClient, user_text: str, system: str,
                   response_schema: Any | None = None) -> tuple[str | None, str | None]:
    """One LLM call; returns (text, None) or (None, error). Budget and LLM
    failures become errors the calling node turns into terminal statuses."""
    try:
        result = llm.generate([user_text], system=system,
                              response_schema=response_schema)
        return result.text, None
    except LLMBudgetExceeded as exc:
        return None, f"budget_exceeded: {exc}"
    except LLMError as exc:
        return None, f"llm_error: {exc}"


def _strip_code_fence(text: str) -> str:
    """Models sometimes wrap JSON in ``` fences despite instructions."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def build_graph(
    llm: LLMClient,
    registry: ToolRegistry,
    *,
    prompt_version: str = "v1",
    checkpointer: Any | None = None,
    run_log: Callable[[dict], None] | None = None,
    min_confidence: float = MIN_CONFIDENCE_FOR_ACTION,
):
    """Compile the dispute-handling graph.

    run_log: called by finalize with the run-record dict (persisted by the
    caller — tests capture it in memory, production writes PostgreSQL).
    min_confidence: confidence floor below which the decision schema forces
    escalate_human. A policy knob (env AGENT_MIN_CONFIDENCE), not a measured
    value — see agent/state.py.
    """
    prompts = {name: load_prompt(name, prompt_version)
               for name in ("draft_response", "critic", "decide_refund")}
    # Tell the model the ACTUAL floor in force — the prompt and the schema
    # validator must never disagree, or the model "passes" a decision the
    # validator then rejects.
    prompts["decide_refund"] = prompts["decide_refund"].replace(
        "{{MIN_CONFIDENCE}}", str(min_confidence))
    # The prompt promises "the schema you are given" — actually give it.
    # Until 2026-11-10 no schema was in the request at all: models guessed
    # field names, and flash-lite guessed wrong (schema_failed, live).
    # The placeholder exists from v5; the replace is a no-op on older pins.
    prompts["decide_refund"] = prompts["decide_refund"].replace(
        "{{DECISION_SCHEMA}}",
        json.dumps(RefundDecision.model_json_schema(), indent=2))

    # -- nodes -----------------------------------------------------------------

    def gather_evidence(state: AgentState) -> dict:
        """Deterministic: pull the dispute + its transaction(s) via MCP tools.
        No LLM — evidence gathering must not be creative."""
        dispute_id = state["dispute_id"]
        res = registry.execute("get_dispute", {"dispute_id": dispute_id})
        if not res.get("ok") or not res["result"].get("ok"):
            err = res.get("error") or res.get("result", {}).get("summary", "get_dispute failed")
            return _fail("evidence_failed", f"get_dispute: {err}")

        dispute = res["result"]["dispute"]
        transactions: dict[str, Any] = {}
        for txn in dispute.get("disputed_transactions", [])[:2]:
            tid = txn.get("seller_transaction_id")
            if not tid:
                continue
            txn_res = registry.execute("get_transaction", {"transaction_id": tid})
            if txn_res.get("ok") and txn_res["result"].get("ok"):
                transactions[tid] = txn_res["result"]["transaction"]
            else:
                transactions[tid] = {"error": txn_res.get("error", "get_transaction failed")}

        if not transactions:
            return _fail("evidence_failed",
                         f"dispute {dispute_id} references no retrievable transaction")

        return {"evidence": {
            "dispute": dispute,
            "transactions": transactions,
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }}

    def draft_response(state: AgentState) -> dict:
        packet = json.dumps(state["evidence"], default=str)
        text, err = _safe_generate(
            llm,
            f"EVIDENCE PACKET:\n{packet}\n\nDispute: {state['dispute_id']}. "
            f"Produce the draft response and refund recommendation.",
            prompts["draft_response"])
        if err:
            return _fail(err.split(":")[0], err)
        return {"draft": text}

    def critic(state: AgentState) -> dict:
        draft = state["draft"]
        evidence_json = json.dumps(state["evidence"], default=str)

        # Half 1 — deterministic scan (cannot be argued with).
        invented_ids = sorted({m for m in _ID_RE.findall(draft) if m not in evidence_json})
        invented_ts = sorted({m for m in _TS_RE.findall(draft) if m not in evidence_json})
        id_violations = (
            [{"claim": i, "problem": "transaction ID not present in the evidence"}
             for i in invented_ids]
            + [{"claim": t, "problem": "timestamp not present in the evidence"}
               for t in invented_ts])

        # Half 2 — LLM fact-check of narrative claims.
        text, err = _safe_generate(
            llm,
            f"EVIDENCE PACKET:\n{evidence_json}\n\nDRAFT:\n{draft}\n\n"
            f"Report violations as JSON.",
            prompts["critic"])
        llm_violations: list[dict] = []
        supported = False
        if err:
            # Fail closed: no critic verdict means no support.
            llm_violations = [{"claim": "<critic call>", "problem": err}]
        else:
            try:
                verdict = json.loads(_strip_code_fence(text))
                llm_violations = list(verdict.get("violations", []))
                supported = bool(verdict.get("supported")) and not llm_violations
            except (ValueError, AttributeError) as exc:
                llm_violations = [{"claim": "<critic output>",
                                   "problem": f"unparseable critic verdict: {exc}"}]

        all_violations = id_violations + llm_violations
        return {"critic_report": {
            "supported": supported and not id_violations,
            "violations": llm_violations,
            "id_violations": id_violations,
        }}

    def decide_refund(state: AgentState) -> dict:
        packet = json.dumps(state["evidence"], default=str)
        text, err = _safe_generate(
            llm,
            f"EVIDENCE PACKET:\n{packet}\n\nCRITIC-APPROVED DRAFT:\n{state['draft']}\n\n"
            f"Dispute: {state['dispute_id']}. Reply with the decision JSON.",
            prompts["decide_refund"],
            response_schema=RefundDecision)
        if err:
            return _fail(err.split(":")[0], err)

        # Validate the decision schema — one pass, fail closed.
        try:
            decision = RefundDecision.model_validate_json(
                _strip_code_fence(text), context={"min_confidence": min_confidence})
        except ValueError as exc:
            return {"decision": None, "decision_error": str(exc),
                    **_fail("schema_failed", f"decision schema validation failed: {exc}")}

        # Deterministic post-checks the schema cannot do alone.
        if decision.dispute_id != state["dispute_id"]:
            return {"decision": None,
                    **_fail("schema_failed",
                            f"decision cites dispute {decision.dispute_id}, "
                            f"run is about {state['dispute_id']}")}
        evidence_json = json.dumps(state["evidence"], default=str)
        for cited in decision.cited_evidence:
            if cited.transaction_id and cited.transaction_id not in evidence_json:
                return {"decision": None,
                        **_fail("schema_failed",
                                f"cited transaction {cited.transaction_id} "
                                f"is not in the evidence")}
        if decision.proposed_action == "issue_refund":
            txn = next(iter(state["evidence"]["transactions"].values()), {})
            txn_amount = (txn.get("amount") or {}).get("value")
            if txn_amount is not None and float(decision.refund_amount) > float(txn_amount):
                return {"decision": None,
                        **_fail("schema_failed",
                                f"refund {decision.refund_amount} exceeds transaction "
                                f"amount {txn_amount}")}

        return {"decision": decision.model_dump(), "decision_error": None}

    def request_refund(state: AgentState) -> dict:
        """Call the gateway's policy-gated refund tool. Deterministic input:
        the idempotency key is derived from the run id, so a resumed or
        retried run can never double-request."""
        decision = state["decision"]
        args = {
            "capture_id": decision["transaction_id"],
            "amount": decision["refund_amount"],
            "currency": decision["currency"],
            "idempotency_key": f"{state['run_id']}-refund",
            "note_to_payer": f"Automated dispute resolution, run {state['run_id']}",
        }
        res = registry.execute("issue_refund", args)
        if not res.get("ok"):
            return {"refund_request": {"kind": "error", "detail": res.get("error")},
                    **_fail("refund_request_failed", str(res.get("error")))}
        kind = interpret_refund_result(res["result"])
        out: dict = {"refund_request": kind}
        if kind["kind"] == "error":
            out.update(_fail("refund_request_failed", str(kind.get("detail"))))
        return out

    def execute_refund(state: AgentState) -> dict:
        """Runs ONLY when the gateway said pending_approval.

        First execution: interrupt() pauses the graph; the checkpointer
        persists everything. The interrupt payload tells the operator what
        to do. On resume, LangGraph RE-RUNS this node from the top — the
        refund request is NOT repeated (it lives in request_refund, an
        already-completed node), and interrupt() now returns the resume
        value instead of pausing.
        """
        request = state["refund_request"]
        approval_id = request["approval_id"]
        resume_value = interrupt({
            "type": "approval_required",
            "run_id": state["run_id"],
            "dispute_id": state["dispute_id"],
            "approval_id": approval_id,
            "action_id": request.get("action_id"),
            "amount": state["decision"]["refund_amount"],
            "currency": state["decision"]["currency"],
            "instructions": (
                "A human must decide this refund via the gateway: "
                "POST /v1/approvals/{id}/decision. Then resume this run with "
                "Command(resume={'approved': true, 'approval_id': <id>}) or "
                "{'approved': false}."),
        })

        approved = bool(resume_value.get("approved")) if isinstance(resume_value, dict) else False
        if not approved:
            return {"refund_result": {"kind": "not_approved", "resume_value": resume_value},
                    "status": "refund_not_approved",
                    "summary": f"Approval #{approval_id} was not granted on resume; no money moved."}

        # The approval_id may come from the resume payload (authoritative —
        # the operator confirmed what they approved).
        resume_approval_id = resume_value.get("approval_id", approval_id)
        res = registry.execute("execute_approved_refund",
                               {"approval_id": resume_approval_id})
        if not res.get("ok") or not res["result"].get("ok"):
            err = res.get("error") or res.get("result", {})
            return {"refund_result": {"kind": "execution_failed", "error": str(err)},
                    **_fail("refund_execution_failed", str(err))}
        refund = res["result"]
        return {"refund_result": {"kind": "executed_after_approval",
                                  "refund": refund.get("refund"),
                                  "summary": refund.get("summary")},
                "status": "refund_executed_after_approval",
                "summary": refund.get("summary", "")}

    def finalize(state: AgentState) -> dict:
        """Terminal bookkeeping: derive the status/summary and write the run log."""
        status = state.get("status", RUNNING)
        if status == RUNNING:
            status, summary = _derive_outcome(state)
        else:
            summary = state.get("error") or state.get("summary") or status

        usage = getattr(llm, "meter", None)
        decision = state.get("decision")
        record = {
            "run_id": state["run_id"],
            "dispute_id": state["dispute_id"],
            "status": status,
            "summary": summary,
            "usage": usage.as_dict() if usage else {},
            # Calibration dataset: what the agent proposed and how sure it
            # was. Human approval/escalation outcomes (gateway side) are the
            # labels; these columns make "confidence vs. correctness" a
            # single query once enough runs exist.
            "confidence": (decision or {}).get("confidence"),
            "proposed_action": (decision or {}).get("proposed_action"),
            "decision": decision,
        }
        if run_log is not None:
            try:
                run_log(record)
            except Exception:  # noqa: BLE001 — logging must not kill the run
                logger.exception("run log write failed for %s", state["run_id"])
        return {"status": status, "summary": summary,
                "usage": record["usage"]}

    def _derive_outcome(state: AgentState) -> tuple[str, str]:
        """No failure status set → derive the terminal outcome from the path taken."""
        request = state.get("refund_request")
        if request is not None:
            kind = request["kind"]
            if kind == "executed":
                return "refund_executed", request.get("summary", "Refund executed.")
            if kind == "denied":
                return ("refund_denied_by_policy",
                        f"Gateway denied the refund ({request.get('reason_code')}).")
            if kind == "deduplicated":
                return ("refund_deduplicated",
                        f"Replay: original outcome was {request.get('original_outcome')}.")
        decision = state.get("decision")
        if decision is not None:
            action = decision["proposed_action"]
            if action == "deny_refund":
                return "decision_deny_refund", f"No refund. {decision['reasoning'][:200]}"
            if action == "escalate_human":
                return "decision_escalate_human", f"Escalated to a human. {decision['reasoning'][:200]}"
        report = state.get("critic_report")
        if report is not None and not report.get("supported"):
            return "rejected_by_critic", "Draft contained unsupported claims; run terminated (one-pass critic, no revision)."
        return "completed_no_action", "Graph finished without a refund path."

    # -- conditional routers -----------------------------------------------------

    def route_after_gather(state: AgentState) -> str:
        return "draft_response" if state.get("status", RUNNING) == RUNNING else "finalize"

    def route_after_draft(state: AgentState) -> str:
        # A budget/LLM failure inside draft_response must skip the critic
        # (which would make another LLM call) and go straight to finalize.
        return "critic" if state.get("status", RUNNING) == RUNNING else "finalize"

    def route_after_critic(state: AgentState) -> str:
        if state.get("status", RUNNING) != RUNNING:
            return "finalize"
        report = state.get("critic_report", {})
        if not report.get("supported"):
            return "finalize"  # one-pass: no revision loop
        return "decide_refund"

    def route_after_decide(state: AgentState) -> str:
        if state.get("status", RUNNING) != RUNNING:
            return "finalize"
        decision = state.get("decision")
        if decision and decision["proposed_action"] == "issue_refund":
            return "request_refund"
        return "finalize"

    def route_after_request(state: AgentState) -> str:
        if state.get("status", RUNNING) != RUNNING:
            return "finalize"
        request = state.get("refund_request") or {}
        if request.get("kind") == "pending_approval":
            return "execute_refund"
        return "finalize"

    # -- wiring -------------------------------------------------------------------

    g = StateGraph(AgentState)
    g.add_node("gather_evidence", gather_evidence)
    g.add_node("draft_response", draft_response)
    g.add_node("critic", critic)
    g.add_node("decide_refund", decide_refund)
    g.add_node("request_refund", request_refund)
    g.add_node("execute_refund", execute_refund)
    g.add_node("finalize", finalize)

    g.add_edge(START, "gather_evidence")
    g.add_conditional_edges("gather_evidence", route_after_gather,
                            ["draft_response", "finalize"])
    # Conditional too: a failure status set by draft_response short-circuits
    # to finalize instead of burning another LLM call on the critic.
    g.add_conditional_edges("draft_response", route_after_draft,
                            ["critic", "finalize"])
    g.add_conditional_edges("critic", route_after_critic,
                            ["decide_refund", "finalize"])
    g.add_conditional_edges("decide_refund", route_after_decide,
                            ["request_refund", "finalize"])
    g.add_conditional_edges("request_refund", route_after_request,
                            ["execute_refund", "finalize"])
    g.add_edge("execute_refund", "finalize")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer)
