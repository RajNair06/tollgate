"""Worker dispatch tests: webhook job -> LangGraph run -> audit row.

The REAL compiled graph runs against the REAL agent_test PostgreSQL
(checkpoints + agent_runs) with ScriptedLLM/FakeRegistry from agent.testing
— the same topology as tests/test_agent_graph.py. Gateway-side rows
(webhook_outbox, audit_log) live in the conftest test database.

Proven here:
- the outcome classification matrix (processed / retryable / permanent)
- happy-path dispatch: job processed, agent_runs row, 'agent_run_finished'
  audit row attributed to the right agent
- approval interrupt: job processed as pending_approval with approval_id
- definite evidence failure (RESOURCE_NOT_FOUND): dead-lettered
- transient evidence failure (GATEWAY_UNREACHABLE): rescheduled
- the duplicate-run guard: a re-dispatched dispute is NOT re-run — the
  checkpoint pre-check returns the recorded outcome
- end to end: signed webhook POST -> outbox row -> worker -> run -> audit
"""

import importlib.util
import json
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent.budget import RunMeter
from agent.checkpoint import ensure_database, open_checkpointer
from agent.config import AgentSettings
from agent.gemini_client import LLMError
from agent.graph import build_graph
from agent.runs import RunLog
from agent.testing import (
    CLEAN_DRAFT,
    CRITIC_OK,
    DISPUTE,
    FakeRegistry,
    ScriptedLLM,
    decision_json,
    envelope_dispute,
    envelope_execute_approved,
    envelope_refund_executed,
    envelope_refund_pending,
    envelope_transaction,
)
from gateway.db import get_engine, session_scope
from gateway.models import AuditLog, WebhookOutbox
from worker.agent_runner import AgentRunner, run_id_for
from worker.config import WorkerSettings
from worker.dispatch import Dispatcher, GatewayAudit, classify_run
from worker.errors import PermanentJobError, RetryableJobError
from worker.queue import enqueue_job
from worker.worker import Worker

AGENT_TEST_DB = "postgresql://tollgate:tollgate@localhost:5433/agent_test"


@pytest.fixture(autouse=True)
def _clean_agent_state():
    """agent_test PERSISTS across pytest runs, and webhook run ids are
    DETERMINISTIC (webhook-<dispute_id>) — without this, a checkpoint left
    by a previous run would make the pre-check skip the run under test.
    Tolerant of fresh databases where the tables don't exist yet."""
    ensure_database(AGENT_TEST_DB)
    with psycopg.connect(AGENT_TEST_DB, autocommit=True) as conn:
        def _exists(table: str) -> bool:
            return conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()[0]
        if _exists("agent_runs"):
            conn.execute("DELETE FROM agent_runs WHERE run_id LIKE 'webhook-%'")
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            if _exists(table):
                conn.execute(
                    f"DELETE FROM {table} WHERE thread_id LIKE 'webhook-%'")
    yield

FAST = WorkerSettings(
    database_url="",  # unused: tests inject the conftest engine
    max_attempts=3,
    backoff_base_seconds=0.02,
    backoff_max_seconds=0.1,
    lease_seconds=300.0,
    poll_interval_seconds=0.01,
    batch_size=5,
)

def _happy_world(dispute_id: str, refund_envelope=None):
    """A self-consistent scripted world: the evidence, the decision JSON,
    and the state all agree on the dispute id (RefundDecision's cross-field
    validator rejects mismatches — schema_failed)."""
    dispute = dict(DISPUTE)
    dispute["dispute_id"] = dispute_id
    responses = {
        "get_dispute": envelope_dispute(dispute),
        "get_transaction": envelope_transaction(),
        "issue_refund": refund_envelope or envelope_refund_executed(),
    }
    script = [CLEAN_DRAFT, CRITIC_OK, decision_json(dispute_id=dispute_id)]
    return script, responses


def _agent_settings() -> AgentSettings:
    return AgentSettings(
        mcp_url="http://unused.invalid/mcp",
        gateway_api_key="tg_test_unused",
        gemini_api_key="dummy",
        model="scripted",
        token_budget=100_000,
        database_url=AGENT_TEST_DB,
        prompt_version="v1",
        min_confidence=0.6,
    )


def _scripted_builder(script, responses, registry=None):
    """graph_builder for AgentRunner: the REAL compiled graph over fakes.
    A shared registry (instead of per-build responses) lets resume tests
    inspect the FULL call history across the pause and the resume."""
    def builder(run_id: str, runner: AgentRunner, prior_usage=None):
        meter = RunMeter(model="scripted", budget_tokens=100_000)
        meter.run_id = run_id
        if prior_usage:
            meter.usage.input_tokens = prior_usage.get("input_tokens", 0)
            meter.usage.output_tokens = prior_usage.get("output_tokens", 0)
            meter.llm_calls = prior_usage.get("llm_calls", 0)
        llm = ScriptedLLM(script, meter=meter)
        reg = registry if registry is not None else FakeRegistry(dict(responses))
        return build_graph(llm, reg, prompt_version="v1",
                           checkpointer=runner.saver,
                           run_log=runner.run_log.write,
                           min_confidence=0.6)
    return builder


@pytest.fixture()
def runner_factory():
    """Build AgentRunners against agent_test, closing them after the test."""
    made: list[AgentRunner] = []

    def _make(script, responses, registry=None):
        r = AgentRunner(_agent_settings(),
                        graph_builder=_scripted_builder(script, responses,
                                                        registry))
        made.append(r)
        return r

    yield _make
    for r in made:
        r.close()


def _enqueue_dispute(dispute_id: str) -> int:
    with session_scope() as s:
        job_id = enqueue_job(s, "dispute_agent",
                             {"dispute_id": dispute_id,
                              "external_event_id": f"WH-{dispute_id}"},
                             f"dispute:{dispute_id}")
    assert job_id is not None
    return job_id


def _job(job_id: int) -> WebhookOutbox:
    with session_scope() as s:
        return s.execute(select(WebhookOutbox).where(
            WebhookOutbox.id == job_id)).scalar_one()


def _latest_run_audit() -> AuditLog | None:
    with session_scope() as s:
        return s.execute(
            select(AuditLog)
            .where(AuditLog.decision == "agent_run_finished")
            .order_by(AuditLog.id.desc()).limit(1)
        ).scalar_one_or_none()


def _agent_run_rows(run_id: str) -> list[tuple]:
    with psycopg.connect(AGENT_TEST_DB, autocommit=True) as conn:
        return conn.execute(
            "SELECT run_id, dispute_id, status, approval_id "
            "FROM agent_runs WHERE run_id = %s",
            (run_id,)).fetchall()


# ---------------------------------------------------------------------------
# Classification matrix (pure function)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,text,expected", [
    # Recorded verdicts -> processed
    ("refund_executed", "", "processed"),
    ("refund_executed_after_approval", "", "processed"),
    ("refund_denied_by_policy", "", "processed"),
    ("refund_deduplicated", "", "processed"),
    ("decision_deny_refund", "", "processed"),
    ("decision_escalate_human", "", "processed"),
    ("completed_no_action", "", "processed"),
    ("schema_failed", "", "processed"),
    ("budget_exceeded", "", "processed"),
    ("pending_approval", "", "processed"),
    ("some_future_status", "", "processed"),  # unknown vocabulary never loops
    # rejected_by_critic: one draft sample; resampling re-runs every money gate
    ("rejected_by_critic", "", "retryable"),
    # LLM/provider failures -> retryable
    ("llm_error", "llm_error: 503 model saturated", "retryable"),
    # Ambiguous statuses: the error text decides
    ("evidence_failed", "get_dispute: RESOURCE_NOT_FOUND: no such dispute",
     "permanent"),
    ("evidence_failed", "get_dispute: GATEWAY_UNREACHABLE: connection refused",
     "retryable"),
    ("evidence_failed", "get_dispute: RATE_LIMITED: slow down", "retryable"),
    ("evidence_failed", "dispute PP-D-1 references no retrievable transaction",
     "permanent"),
    ("refund_request_failed", "PayPalServerError: [503] outage", "retryable"),
    ("refund_request_failed", "NOT_ALLOWED: action not in allowlist",
     "permanent"),
    ("refund_execution_failed", "request timed out", "retryable"),
    ("refund_execution_failed", "INVALID_ARGUMENT: bad capture id",
     "permanent"),
])
def test_classify_run_matrix(status, text, expected):
    assert classify_run(status, text) == expected


# ---------------------------------------------------------------------------
# Dispatcher guards
# ---------------------------------------------------------------------------

def test_unknown_job_type_is_permanent():
    called = []
    d = Dispatcher(lambda dispute_id: called.append(dispute_id) or {})
    with pytest.raises(PermanentJobError, match="RESUME_NOT_CONFIGURED"):
        d("resume_agent", {})  # this dispatcher has no resume handler wired
    with pytest.raises(PermanentJobError, match="UNKNOWN_JOB_TYPE"):
        d("frobnicate", {})
    assert called == []


def test_missing_dispute_id_is_permanent():
    d = Dispatcher(lambda dispute_id: {})
    with pytest.raises(PermanentJobError, match="MALFORMED_PAYLOAD"):
        d("dispute_agent", {"nope": 1})


# ---------------------------------------------------------------------------
# Dispatch through the worker
# ---------------------------------------------------------------------------

def test_happy_path_processes_run_and_audits(runner_factory, make_agent,
                                             monkeypatch):
    agent_id, api_key = make_agent()
    monkeypatch.setenv("AGENT_API_KEY", api_key)
    script, responses = _happy_world("PP-D-77001")
    runner = runner_factory(script, responses)
    dispatcher = Dispatcher(runner.run_dispute,
                            audit=GatewayAudit(get_engine(), api_key))
    job_id = _enqueue_dispute("PP-D-77001")

    assert Worker(get_engine(), dispatcher, FAST).run_once() == 1

    row = _job(job_id)
    assert row.status == "processed"
    assert row.processed_at is not None

    # The run reached the agent's own database under the deterministic id.
    run_id = run_id_for("PP-D-77001")
    assert run_id == "webhook-PP-D-77001"
    rows = _agent_run_rows(run_id)
    assert len(rows) == 1
    assert rows[0][2] == "refund_executed"

    # The final outcome is in the gateway's audit_log, attributed correctly.
    audit = _latest_run_audit()
    assert audit is not None
    assert audit.reason_code == "refund_executed"
    assert audit.agent_id == agent_id
    assert audit.detail["run_id"] == run_id
    assert audit.detail["dispute_id"] == "PP-D-77001"


def test_approval_interrupt_processed_as_pending_approval(runner_factory,
                                                          make_agent):
    _, api_key = make_agent()
    script, responses = _happy_world(
        "PP-D-77002",
        refund_envelope=envelope_refund_pending(approval_id=42, action_id=680))
    runner = runner_factory(script, responses)
    dispatcher = Dispatcher(runner.run_dispute,
                            audit=GatewayAudit(get_engine(), api_key))
    job_id = _enqueue_dispute("PP-D-77002")

    Worker(get_engine(), dispatcher, FAST).run_once()

    assert _job(job_id).status == "processed"  # the pause is not a failure
    audit = _latest_run_audit()
    assert audit.reason_code == "pending_approval"
    assert audit.detail["approval_id"] == 42


def test_definite_evidence_failure_dead_letters(runner_factory, make_agent):
    _, api_key = make_agent()
    responses = {"get_dispute": {
        "ok": False, "summary": "RESOURCE_NOT_FOUND: dispute does not exist"}}
    runner = runner_factory([], responses)  # no LLM call should ever happen
    dispatcher = Dispatcher(runner.run_dispute,
                            audit=GatewayAudit(get_engine(), api_key))
    job_id = _enqueue_dispute("PP-D-77003")

    Worker(get_engine(), dispatcher, FAST).run_once()

    row = _job(job_id)
    assert row.status == "dead"
    assert row.attempts == 1
    assert row.failed_at is not None
    assert "RESOURCE_NOT_FOUND" in row.last_error
    # Even the failure outcome is audited.
    assert _latest_run_audit().reason_code == "evidence_failed"


def test_transient_evidence_failure_reschedules(runner_factory, make_agent):
    _, api_key = make_agent()
    responses = {"get_dispute": {
        "ok": False, "summary": "GATEWAY_UNREACHABLE: connection refused"}}
    runner = runner_factory([], responses)
    dispatcher = Dispatcher(runner.run_dispute,
                            audit=GatewayAudit(get_engine(), api_key))
    job_id = _enqueue_dispute("PP-D-77004")

    Worker(get_engine(), dispatcher, FAST).run_once()

    row = _job(job_id)
    assert row.status == "pending"  # retried later, not dead
    assert row.attempts == 1
    assert "GATEWAY_UNREACHABLE" in row.last_error


def test_redispatch_of_finished_dispute_is_not_rerun(runner_factory):
    """The duplicate-run guard: at-least-once dispatch + checkpoint
    pre-check = one RUN per dispute. The second runner has an EMPTY LLM
    script — if the graph re-ran, ScriptedLLM would raise."""
    first = runner_factory(*_happy_world("PP-D-77005"))
    outcome1 = first.run_dispute("PP-D-77005")
    assert outcome1["status"] == "refund_executed"
    assert outcome1["run_id"] == "webhook-PP-D-77005"

    second = runner_factory([], {})  # any LLM/tool call would blow up
    outcome2 = second.run_dispute("PP-D-77005")

    assert outcome2["already_completed"] is True
    assert outcome2["status"] == "refund_executed"
    assert len(_agent_run_rows("webhook-PP-D-77005")) == 1  # one run record


def test_transient_llm_failure_reruns_instead_of_caching(runner_factory):
    """Live-rehearsal bug (2026-11-10): an llm_error checkpoint used to
    short-circuit every job retry ("already finished (llm_error) — not
    re-running"), burning all 5 attempts with zero LLM calls before
    dead-lettering. A retryable terminal status must clear the thread and
    re-run for real; verdict statuses stay cached (test above)."""
    _, responses = _happy_world("PP-D-77010")
    broken = runner_factory([LLMError("503 UNAVAILABLE: high demand")],
                            responses)
    out1 = broken.run_dispute("PP-D-77010")
    assert out1["status"] == "llm_error"

    # The job retry: the provider recovered and a full happy script is
    # available — the run must actually execute it, not replay the cache.
    script, responses = _happy_world("PP-D-77010")
    healthy = runner_factory(script, responses)
    out2 = healthy.run_dispute("PP-D-77010")
    assert out2.get("already_completed") is not True
    assert out2["status"] == "refund_executed"
    assert len(_agent_run_rows("webhook-PP-D-77010")) == 1


def test_redispatch_of_paused_dispute_reports_pending_again(runner_factory):
    first = runner_factory(*_happy_world(
        "PP-D-77006",
        refund_envelope=envelope_refund_pending(approval_id=43)))
    outcome1 = first.run_dispute("PP-D-77006")
    assert outcome1["status"] == "pending_approval"
    assert outcome1["interrupt"]["approval_id"] == 43

    second = runner_factory([], {})
    outcome2 = second.run_dispute("PP-D-77006")
    assert outcome2["already_completed"] is True
    assert outcome2["status"] == "pending_approval"
    assert outcome2["interrupt"]["approval_id"] == 43  # pause undisturbed


# ---------------------------------------------------------------------------
# Resume pipeline: approval decision -> resume_agent job -> money moves once
# ---------------------------------------------------------------------------

def _pause_world(dispute_id: str, approval_id: int):
    return _happy_world(dispute_id, refund_envelope=envelope_refund_pending(
        approval_id=approval_id, action_id=600 + approval_id))


def _enqueue_resume(approval_id: int, approved: bool,
                    decided_by: str = "admin-test") -> int:
    with session_scope() as s:
        job_id = enqueue_job(s, "resume_agent",
                             {"approval_id": approval_id,
                              "approved": approved,
                              "decided_by": decided_by})
    assert job_id is not None
    return job_id


def _resume_dispatcher(runner, api_key) -> Dispatcher:
    return Dispatcher(runner.run_dispute, resume=runner.resume_approval,
                      audit=GatewayAudit(get_engine(), api_key))


def test_pause_writes_provisional_run_row(runner_factory):
    """finalize() never runs at an interrupt — the pause itself must write
    the agent_runs row (with approval_id), or resume lookup is blind."""
    script, responses = _pause_world("PP-D-77011", approval_id=77)
    runner = runner_factory(script, responses)

    outcome = runner.run_dispute("PP-D-77011")

    assert outcome["status"] == "pending_approval"
    rows = _agent_run_rows("webhook-PP-D-77011")
    assert len(rows) == 1
    assert rows[0][2] == "pending_approval"
    assert rows[0][3] == 77  # the resume lookup key
    # Found by approval id:
    assert runner.run_log.find_by_approval(77)["run_id"] == "webhook-PP-D-77011"
    # The FULL decision (reasoning + cited evidence) is preserved in the
    # record's detail JSON — the dashboard's approvals panel shows the
    # human WHY the agent wants to refund while the run is still paused.
    with psycopg.connect(AGENT_TEST_DB, autocommit=True) as conn:
        detail = conn.execute(
            "SELECT detail FROM agent_runs WHERE run_id = 'webhook-PP-D-77011'"
        ).fetchone()[0]
    assert detail["decision"]["reasoning"]
    assert detail["decision"]["cited_evidence"]


def test_resume_approved_executes_refund(runner_factory, make_agent):
    _, api_key = make_agent()
    script, responses = _pause_world("PP-D-77012", approval_id=78)
    responses = {**responses,
                 "execute_approved_refund": envelope_execute_approved()}
    registry = FakeRegistry(responses)  # shared across pause + resume builds
    runner = runner_factory(script, responses, registry=registry)
    assert runner.run_dispute("PP-D-77012")["status"] == "pending_approval"

    job_id = _enqueue_resume(78, approved=True)
    Worker(get_engine(), _resume_dispatcher(runner, api_key), FAST).run_once()

    assert _job(job_id).status == "processed"
    # The money moved through the gateway tool, once, for the right approval.
    assert ("execute_approved_refund", {"approval_id": 78}) in registry.calls
    rows = _agent_run_rows("webhook-PP-D-77012")
    assert len(rows) == 1  # upsert, not a second row
    assert rows[0][2] == "refund_executed_after_approval"
    assert rows[0][3] == 78  # COALESCE kept the link through finalize
    audit = _latest_run_audit()
    assert audit.reason_code == "refund_executed_after_approval"
    assert audit.detail["dispute_id"] == "PP-D-77012"


def test_resume_rejected_moves_no_money(runner_factory, make_agent):
    _, api_key = make_agent()
    script, responses = _pause_world("PP-D-77013", approval_id=79)
    registry = FakeRegistry(responses)
    runner = runner_factory(script, responses, registry=registry)
    runner.run_dispute("PP-D-77013")

    job_id = _enqueue_resume(79, approved=False)
    Worker(get_engine(), _resume_dispatcher(runner, api_key), FAST).run_once()

    assert _job(job_id).status == "processed"  # a rejection is a verdict
    assert "execute_approved_refund" not in registry.names_called()
    rows = _agent_run_rows("webhook-PP-D-77013")
    assert rows[0][2] == "refund_not_approved"
    assert _latest_run_audit().reason_code == "refund_not_approved"


def test_resume_unknown_approval_dead_letters(runner_factory, make_agent):
    _, api_key = make_agent()
    runner = runner_factory([], {})
    job_id = _enqueue_resume(987654321, approved=True)

    Worker(get_engine(), _resume_dispatcher(runner, api_key), FAST).run_once()

    row = _job(job_id)
    assert row.status == "dead"
    assert "NO_RUN_FOR_APPROVAL" in row.last_error


def test_resume_redelivery_moves_money_once(runner_factory, make_agent):
    """A reclaimed lease / duplicate enqueue delivers the same resume again.
    The checkpoint pre-check makes the second delivery a recorded no-op."""
    _, api_key = make_agent()
    script, responses = _pause_world("PP-D-77014", approval_id=80)
    responses = {**responses,
                 "execute_approved_refund": envelope_execute_approved()}
    registry = FakeRegistry(responses)
    runner = runner_factory(script, responses, registry=registry)
    runner.run_dispute("PP-D-77014")

    worker = Worker(get_engine(), _resume_dispatcher(runner, api_key), FAST)
    _enqueue_resume(80, approved=True)
    worker.run_once()
    _enqueue_resume(80, approved=True)  # redelivery of the same decision
    worker.run_once()

    executions = [c for c in registry.calls
                  if c[0] == "execute_approved_refund"]
    assert len(executions) == 1  # money moved exactly once
    assert _agent_run_rows("webhook-PP-D-77014")[0][2] == \
        "refund_executed_after_approval"


def test_resume_wrong_approval_id_is_permanent(runner_factory):
    """The run is paused for approval 81; a resume claiming 82 must not
    move money on it — even though a run row was doctored to point there."""
    script, responses = _pause_world("PP-D-77015", approval_id=81)
    runner = runner_factory(script, responses)
    runner.run_dispute("PP-D-77015")
    with psycopg.connect(AGENT_TEST_DB, autocommit=True) as conn:
        conn.execute("UPDATE agent_runs SET approval_id = 82 "
                     "WHERE run_id = 'webhook-PP-D-77015'")

    with pytest.raises(PermanentJobError, match="APPROVAL_MISMATCH"):
        runner.resume_approval(82, approved=True, decided_by="typo-operator")


@pytest.mark.parametrize("payload", [
    {"approved": True},                      # missing approval_id
    {"approval_id": "x", "approved": True},  # wrong type
    {"approval_id": True, "approved": True},  # bool is not an id
    {"approval_id": 5},                      # missing decision
    {"approval_id": 5, "approved": "yes"},   # wrong type
])
def test_resume_payload_validation_is_permanent(payload):
    d = Dispatcher(lambda dispute_id: {}, resume=lambda *a: {})
    with pytest.raises(PermanentJobError, match="MALFORMED_PAYLOAD"):
        d("resume_agent", payload)


# ---------------------------------------------------------------------------
# The full chain: signed webhook -> outbox -> worker -> run -> audit
# ---------------------------------------------------------------------------

def _load_simulator():
    path = (Path(__file__).resolve().parent.parent
            / "scripts" / "simulate_dispute.py")
    spec = importlib.util.spec_from_file_location("simulate_dispute", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_webhook_to_worker_end_to_end(runner_factory, make_agent, monkeypatch):
    secret = "e2e-shared-secret"
    _, api_key = make_agent()
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", secret)
    monkeypatch.setenv("AGENT_API_KEY", api_key)

    # 1. A signed dispute event arrives at the real endpoint.
    from main import app
    sim = _load_simulator()
    payload = sim.build_payload(dispute_id="PP-D-77007",
                                event_id="WH-E2E-1")
    raw = json.dumps(payload).encode()
    with TestClient(app) as client:
        r = client.post("/webhooks/paypal", content=raw, headers={
            "Content-Type": "application/json",
            "X-Tollgate-Signature": sim.sign(raw, secret)})
    assert r.status_code == 200 and r.json()["job_created"] is True

    # 2. The worker claims and dispatches it (scripted graph, real everything else).
    script, responses = _happy_world("PP-D-77007")
    runner = runner_factory(script, responses)
    dispatcher = Dispatcher(runner.run_dispute,
                            audit=GatewayAudit(get_engine(), api_key))
    assert Worker(get_engine(), dispatcher, FAST).run_once() == 1

    # 3. Every ledger agrees.
    with session_scope() as s:
        job = s.execute(select(WebhookOutbox)).scalar_one()
    assert job.status == "processed"
    assert job.dedup_key == "dispute:PP-D-77007"
    rows = _agent_run_rows("webhook-PP-D-77007")
    assert len(rows) == 1 and rows[0][2] == "refund_executed"
    audit = _latest_run_audit()
    assert audit.reason_code == "refund_executed"
    assert audit.detail["dispute_id"] == "PP-D-77007"

    # 4. A duplicate delivery of the SAME event changes nothing.
    with TestClient(app) as client:
        r2 = client.post("/webhooks/paypal", content=raw, headers={
            "Content-Type": "application/json",
            "X-Tollgate-Signature": sim.sign(raw, secret)})
    assert r2.json()["status"] == "duplicate"
    with session_scope() as s:
        n = len(s.execute(select(WebhookOutbox)).scalars().all())
    assert n == 1  # still exactly one job — one run per dispute
