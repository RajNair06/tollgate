"""Phase C tests: the LangGraph dispute agent.

Everything here runs the REAL compiled graph with a REAL PostgreSQL
checkpointer (agent_test database) — only the LLM and the gateway are
scripted (ScriptedLLM / FakeRegistry from agent.testing).

The last test is the one that matters most for durability: it runs the
graph in a SUBPROCESS until the approval interrupt, kills the process, and
resumes in a SECOND subprocess — state carried exclusively by PostgreSQL.
That is the 'restart the agent container' proof.
"""

import json
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from langgraph.types import Command

from agent.budget import LLMBudgetExceeded, RunMeter
from agent.checkpoint import ensure_database, open_checkpointer
from agent.graph import build_graph
from agent.runs import RunLog
from agent.state import RefundDecision
from agent.testing import (
    CAPTURE_ID,
    CLEAN_DRAFT,
    CRITIC_OK,
    DISPUTE_ID,
    FakeRegistry,
    ScriptedLLM,
    decision_json,
    envelope_dispute,
    envelope_execute_approved,
    envelope_refund_denied,
    envelope_refund_executed,
    envelope_refund_pending,
    envelope_transaction,
)

AGENT_TEST_DB = "postgresql://tollgate:tollgate@localhost:5433/agent_test"
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def agent_db():
    """Create + migrate the agent test database once per module."""
    ensure_database(AGENT_TEST_DB)
    RunLog(AGENT_TEST_DB).init()
    conn, saver = open_checkpointer(AGENT_TEST_DB)
    yield saver
    conn.close()


def make_graph(saver, llm_script, responses, *, meter=None, run_log=None,
               min_confidence=None):
    llm = ScriptedLLM(llm_script, meter=meter)
    registry = FakeRegistry(responses)
    records = []
    extra = {} if min_confidence is None else {"min_confidence": min_confidence}
    graph = build_graph(llm, registry, prompt_version="v1",
                        checkpointer=saver,
                        run_log=run_log or records.append, **extra)
    return graph, llm, registry, records


def fresh_state(dispute_id=DISPUTE_ID):
    run_id = f"run-t{uuid.uuid4().hex[:12]}"
    return ({"run_id": run_id, "dispute_id": dispute_id, "status": "running"},
            {"configurable": {"thread_id": run_id}}, run_id)


HAPPY_RESPONSES = {
    "get_dispute": envelope_dispute(),
    "get_transaction": envelope_transaction(),
}


# ---------------------------------------------------------------------------
# Happy paths and decision routing
# ---------------------------------------------------------------------------

def test_happy_path_refund_executed(agent_db):
    graph, llm, registry, records = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, decision_json()],
        {**HAPPY_RESPONSES, "issue_refund": envelope_refund_executed()})
    state, config, run_id = fresh_state()

    result = graph.invoke(state, config)

    assert result["status"] == "refund_executed"
    assert "92F13256LW3371821" in result["summary"]
    assert registry.names_called() == [
        "get_dispute", "get_transaction", "issue_refund"]
    # The idempotency key is derived from the run id — deterministic.
    refund_args = registry.calls[-1][1]
    assert refund_args["idempotency_key"] == f"{run_id}-refund"
    assert refund_args["capture_id"] == CAPTURE_ID
    assert refund_args["amount"] == "12.99"

    assert len(records) == 1
    assert records[0]["run_id"] == run_id
    assert records[0]["status"] == "refund_executed"


def test_decide_call_enforces_schema_and_v5_prompt_renders_it(agent_db):
    """Schema enforcement (2026-11-10, live schema_failed): the prompt said
    "matching the schema you are given" but nothing ever sent a schema —
    flash-lite invented field names ({"decision": ...} for "proposed_action").
    Now the decide_refund call passes RefundDecision as response_schema
    (constrained decoding forbids wrong field names at the API), and v5+
    prompts carry the rendered schema in the text too."""
    llm = ScriptedLLM([CLEAN_DRAFT, CRITIC_OK, decision_json()])
    registry = FakeRegistry({**HAPPY_RESPONSES,
                             "issue_refund": envelope_refund_executed()})
    graph = build_graph(llm, registry, prompt_version="v5",
                        checkpointer=agent_db, run_log=lambda rec: None)
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "refund_executed"
    # draft + critic: free text; decide: schema-enforced
    assert llm.response_schemas_seen == [None, None, RefundDecision]
    decide_prompt = llm.prompts_seen[2]
    assert "proposed_action" in decide_prompt           # schema rendered
    assert "{{DECISION_SCHEMA}}" not in decide_prompt   # placeholder replaced
    assert "{{MIN_CONFIDENCE}}" not in decide_prompt


def test_deny_decision_never_calls_the_gateway(agent_db):
    graph, _, registry, _ = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, decision_json(action="deny_refund")],
        HAPPY_RESPONSES)
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "decision_deny_refund"
    assert "issue_refund" not in registry.names_called()


def test_escalate_decision_routes_to_human(agent_db):
    graph, _, registry, _ = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK,
         decision_json(action="escalate_human", confidence=0.4)],
        HAPPY_RESPONSES)
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "decision_escalate_human"
    assert "issue_refund" not in registry.names_called()


def test_policy_denial_is_a_terminal_status(agent_db):
    graph, _, registry, _ = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, decision_json()],
        {**HAPPY_RESPONSES, "issue_refund": envelope_refund_denied()})
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "refund_denied_by_policy"
    assert "PER_TXN_CAP_EXCEEDED" in result["summary"]


# ---------------------------------------------------------------------------
# Interrupt + resume (in process, then cross-process)
# ---------------------------------------------------------------------------

def test_pending_approval_interrupts_the_graph(agent_db):
    graph, _, registry, records = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, decision_json()],
        {**HAPPY_RESPONSES, "issue_refund": envelope_refund_pending(approval_id=4)})
    state, config, run_id = fresh_state()

    result = graph.invoke(state, config)

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["type"] == "approval_required"
    assert payload["approval_id"] == 4
    assert payload["run_id"] == run_id
    # No money moved, nothing executed, run not finalized.
    assert "execute_approved_refund" not in registry.names_called()
    assert records == []
    # The pause is durable: the checkpoint holds the pending interrupt.
    snap = graph.get_state(config)
    assert snap.next  # there is a next step waiting


def test_resume_in_process_executes_approved_refund(agent_db):
    graph, _, registry, records = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, decision_json()],
        {**HAPPY_RESPONSES,
         "issue_refund": envelope_refund_pending(approval_id=7),
         "execute_approved_refund": envelope_execute_approved()})
    state, config, _ = fresh_state()
    paused = graph.invoke(state, config)
    assert "__interrupt__" in paused

    result = graph.invoke(Command(resume={"approved": True, "approval_id": 7}),
                          config)

    assert result["status"] == "refund_executed_after_approval"
    assert ("execute_approved_refund", {"approval_id": 7}) in registry.calls
    assert records[-1]["status"] == "refund_executed_after_approval"


def test_resume_with_rejection_moves_no_money(agent_db):
    graph, _, registry, records = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, decision_json()],
        {**HAPPY_RESPONSES,
         "issue_refund": envelope_refund_pending(approval_id=8),
         "execute_approved_refund": envelope_execute_approved()})
    state, config, _ = fresh_state()
    graph.invoke(state, config)

    result = graph.invoke(Command(resume={"approved": False}), config)

    assert result["status"] == "refund_not_approved"
    assert "execute_approved_refund" not in registry.names_called()
    assert records[-1]["status"] == "refund_not_approved"


def test_resume_survives_a_process_restart(agent_db):
    """THE durability proof: run to the interrupt in one process, resume in
    a fresh one. Nothing but PostgreSQL connects the two."""
    script = {
        "llm": [CLEAN_DRAFT, CRITIC_OK, decision_json()],
        "responses": {
            "get_dispute": envelope_dispute(),
            "get_transaction": envelope_transaction(),
            "issue_refund": envelope_refund_pending(approval_id=9),
            "execute_approved_refund": envelope_execute_approved(),
        },
    }
    script_path = Path("/tmp/opencode/agent_child_script.json")
    script_path.write_text(json.dumps(script))
    thread_id = f"run-restart-{uuid.uuid4().hex[:8]}"
    child = REPO_ROOT / "tests" / "agent_child.py"

    proc1 = subprocess.run(
        [sys.executable, str(child), AGENT_TEST_DB, "run", thread_id, str(script_path)],
        capture_output=True, text=True, timeout=120, cwd=REPO_ROOT)
    assert proc1.returncode == 0, proc1.stderr
    out1 = json.loads(proc1.stdout)
    assert out1["paused"] is True
    assert out1["interrupt"]["approval_id"] == 9

    # Process 1 is gone. A brand-new process resumes from the checkpoint.
    proc2 = subprocess.run(
        [sys.executable, str(child), AGENT_TEST_DB, "resume", thread_id, str(script_path)],
        capture_output=True, text=True, timeout=120, cwd=REPO_ROOT)
    assert proc2.returncode == 0, proc2.stderr
    out2 = json.loads(proc2.stdout)
    assert out2["status"] == "refund_executed_after_approval"

    # And the checkpoint rows really live in PostgreSQL.
    with psycopg.connect(AGENT_TEST_DB) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s",
            (thread_id,)).fetchone()
    assert rows[0] > 0


# ---------------------------------------------------------------------------
# Critic: deterministic hallucination guards + LLM verdict, one pass
# ---------------------------------------------------------------------------

def test_invented_transaction_id_is_rejected(agent_db):
    fake_id = "QQQQQQQQQQQQQQQQ1"  # 17 chars, nowhere in the evidence
    dirty_draft = (f"Buyer paid for capture {CAPTURE_ID} and also {fake_id}. "
                   f"REFUND RECOMMENDATION: FULL REFUND of 12.99 USD.")
    graph, llm, registry, records = make_graph(
        agent_db,
        [dirty_draft, CRITIC_OK, decision_json()],  # critic SAYS supported...
        {**HAPPY_RESPONSES, "issue_refund": envelope_refund_executed()})
    state, config, _ = fresh_state()

    result = graph.invoke(state, config)

    # ...the deterministic scan overrides it. One pass — no revision loop.
    assert result["status"] == "rejected_by_critic"
    assert any(fake_id in v["claim"] for v in result["critic_report"]["id_violations"])
    assert "issue_refund" not in registry.names_called()
    assert llm.prompts_seen[1].startswith("You are a fact-checking critic")
    # decide_refund never ran → its scripted response is still unconsumed.
    assert len(llm.script) == 1


def test_invented_timestamp_is_rejected(agent_db):
    dirty_draft = ("Delivery confirmed 2027-01-01T00:00:00Z. "
                   "REFUND RECOMMENDATION: NO REFUND.")
    graph, _, registry, _ = make_graph(
        agent_db,
        [dirty_draft, CRITIC_OK, decision_json(action="deny_refund")],
        HAPPY_RESPONSES)
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "rejected_by_critic"
    assert any("2027-01-01" in v["claim"]
               for v in result["critic_report"]["id_violations"])


def test_llm_critic_violations_block_the_run(agent_db):
    verdict = json.dumps({"supported": False, "violations": [
        {"claim": "the buyer received the item",
         "problem": "evidence contains no delivery confirmation"}]})
    graph, _, registry, _ = make_graph(
        agent_db,
        [CLEAN_DRAFT, verdict, decision_json()],
        {**HAPPY_RESPONSES, "issue_refund": envelope_refund_executed()})
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "rejected_by_critic"
    assert result["critic_report"]["violations"][0]["problem"].startswith("evidence")
    assert "issue_refund" not in registry.names_called()


def test_unparseable_critic_verdict_fails_closed(agent_db):
    graph, _, _, _ = make_graph(
        agent_db,
        [CLEAN_DRAFT, "I think the draft looks fine!", decision_json()],
        HAPPY_RESPONSES)
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "rejected_by_critic"
    assert "unparseable" in result["critic_report"]["violations"][0]["problem"]


# ---------------------------------------------------------------------------
# Decision schema validation (fail closed)
# ---------------------------------------------------------------------------

def test_non_json_decision_fails_schema(agent_db):
    graph, _, registry, _ = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, "We should probably refund the buyer."],
        {**HAPPY_RESPONSES, "issue_refund": envelope_refund_executed()})
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "schema_failed"
    assert result["decision"] is None
    assert "issue_refund" not in registry.names_called()


def test_decision_for_wrong_dispute_fails_schema(agent_db):
    graph, _, _, _ = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, decision_json(dispute_id="PP-D-99999")],
        HAPPY_RESPONSES)
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "schema_failed"
    assert "PP-D-99999" in result["error"]


def test_refund_above_transaction_amount_fails_schema(agent_db):
    graph, _, _, _ = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, decision_json(amount="99.99")],
        HAPPY_RESPONSES)
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "schema_failed"
    assert "exceeds transaction" in result["error"]


def test_low_confidence_auto_action_fails_schema(agent_db):
    # issue_refund at confidence 0.4 violates MIN_CONFIDENCE_FOR_ACTION.
    graph, _, _, _ = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, decision_json(confidence=0.4)],
        HAPPY_RESPONSES)
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "schema_failed"
    assert "escalate_human" in result["error"]


def test_min_confidence_floor_is_configurable(agent_db):
    # The 0.4 + issue_refund that fails under the default floor passes when
    # the deployment lowers it...
    graph, _, _, _ = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, decision_json(confidence=0.4)],
        {**HAPPY_RESPONSES, "issue_refund": envelope_refund_executed()},
        min_confidence=0.3)
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "refund_executed"

    # ...and a normally-fine 0.9 is caught by a stricter floor. The error
    # quotes the EFFECTIVE threshold, so prompt and validator can't drift.
    graph, _, _, _ = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, decision_json(confidence=0.9)],
        HAPPY_RESPONSES,
        min_confidence=0.95)
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "schema_failed"
    assert "0.95" in result["error"]


def test_cited_transaction_must_exist_in_evidence(agent_db):
    # Valid-looking citation of a transaction that was never retrieved.
    decision = json.loads(decision_json())
    decision["cited_evidence"][0]["transaction_id"] = "ZZZZZZZZZZZZZZZZ2"
    decision["transaction_id"] = CAPTURE_ID
    graph, _, _, _ = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, json.dumps(decision)],
        HAPPY_RESPONSES)
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "schema_failed"
    assert "not in the evidence" in result["error"]


# ---------------------------------------------------------------------------
# Evidence gathering + budget
# ---------------------------------------------------------------------------

def test_evidence_failure_terminates_before_any_llm_call(agent_db):
    graph, llm, _, records = make_graph(
        agent_db,
        [],  # any LLM call would explode — none may happen
        # Exception → FakeRegistry returns {"ok": False, "error": ...}, the
        # same shape the real registry produces for a gateway ToolError.
        {"get_dispute": Exception("RESOURCE_NOT_FOUND: no such dispute")})
    state, config, _ = fresh_state(dispute_id="PP-D-1")
    result = graph.invoke(state, config)
    assert result["status"] == "evidence_failed"
    assert "RESOURCE_NOT_FOUND" in result["error"]
    assert llm.prompts_seen == []
    assert records[0]["status"] == "evidence_failed"


def test_budget_exhaustion_terminates_with_recorded_status(agent_db):
    graph, _, _, records = make_graph(
        agent_db,
        [LLMBudgetExceeded("run out of tokens")],
        HAPPY_RESPONSES,
        meter=RunMeter(model="gemini-2.5-flash", budget_tokens=10))
    state, config, _ = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "budget_exceeded"
    assert records[0]["status"] == "budget_exceeded"


def test_run_log_row_lands_in_postgres(agent_db):
    run_log = RunLog(AGENT_TEST_DB)
    meter = RunMeter(model="gemini-2.5-flash", budget_tokens=100000)
    graph, _, _, _ = make_graph(
        agent_db,
        [CLEAN_DRAFT, CRITIC_OK, decision_json()],
        {**HAPPY_RESPONSES, "issue_refund": envelope_refund_executed()},
        meter=meter, run_log=run_log.write)
    state, config, run_id = fresh_state()
    result = graph.invoke(state, config)
    assert result["status"] == "refund_executed"

    with psycopg.connect(AGENT_TEST_DB) as conn:
        row = conn.execute(
            "SELECT status, input_tokens, output_tokens, cost_usd, llm_calls, "
            "confidence, proposed_action "
            "FROM agent_runs WHERE run_id = %s", (run_id,)).fetchone()
    assert row is not None
    status, in_tok, out_tok, cost, calls, confidence, proposed = row
    assert status == "refund_executed"
    assert in_tok == 300 and out_tok == 150 and calls == 3  # 3 LLM nodes
    assert float(cost) > 0
    # Calibration columns: what the agent proposed and how sure it claimed
    # to be — the raw material for tuning the floor once labels exist.
    assert confidence == pytest.approx(0.9)
    assert proposed == "issue_refund"
    run_log.dispose()
