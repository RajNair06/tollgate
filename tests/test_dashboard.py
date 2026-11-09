"""Dashboard read-API tests.

Gateway-side rows are seeded through the REAL policy engine (decide
fixture), so actions/approvals/audit are exactly what production writes.
Agent-side rows (agent_runs) are seeded directly into agent_test — the
cross-DB join is what's under test, not the graph (that's covered in
test_worker_dispatch.py).

The agent_runs table is wiped per test in THIS module: metrics assertions
need deterministic counts, and agent_test persists across runs.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.db import session_scope
from gateway.models import WebhookEvent, WebhookOutbox
from gateway.policy_engine import ActionRequest

AGENT_TEST_DB = "postgresql://tollgate:tollgate@localhost:5433/agent_test"
ADMIN_KEY = "tga_" + "test_" + "d" * 40
AH = {"X-Admin-Key": ADMIN_KEY}
BASE = "/v1/admin/dashboard"

# A deliberately LONG error text — the dashboard's no-truncation contract
# starts server-side: the API must return it whole.
LONG_ERROR = (
    "PermanentJobError: evidence_failed: get_dispute: RESOURCE_NOT_FOUND: "
    "The specified resource does not exist. " + "detail-" * 60
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)
    monkeypatch.setenv("AGENT_DATABASE_URL", AGENT_TEST_DB)


@pytest.fixture(autouse=True)
def _clean_agent_runs():
    with psycopg.connect(AGENT_TEST_DB, autocommit=True) as conn:
        conn.execute("DELETE FROM agent_runs")
    yield


@pytest.fixture()
def client():
    from main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

def req(amount: str, key: str | None = None) -> ActionRequest:
    return ActionRequest(action_type="refund", amount=Decimal(amount),
                         currency="USD", idempotency_key=key or uuid4().hex)


def _make_pending(decide, make_agent, amount: str = "75.00") -> tuple[int, int]:
    agent_id, _ = make_agent(threshold="50.00")
    d = decide(agent_id, req(amount))
    assert d.outcome == "pending_approval"
    return agent_id, d.approval_id


def _seed_agent_run(run_id: str, approval_id: int | None, status: str,
                    *, reasoning: str | None = None,
                    confidence: float | None = None,
                    proposed: str | None = None,
                    age_seconds: int = 0, duration_seconds: int = 0):
    detail = {}
    if reasoning is not None:
        detail["decision"] = {
            "action": proposed, "confidence": confidence,
            "reasoning": reasoning,
            "cited_evidence": [
                {"source": "transaction", "claim": "capture completed for 75.00 USD",
                 "value": "75.00", "transaction_id": "05Y454980K6595141",
                 "observed_at": "2026-10-28T09:00:00Z"},
                {"source": "dispute", "claim": "reason code on record",
                 "value": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"},
            ],
        }
    with psycopg.connect(AGENT_TEST_DB, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO agent_runs
                   (run_id, dispute_id, status, summary, input_tokens,
                    output_tokens, total_tokens, cost_usd, llm_calls,
                    confidence, proposed_action, approval_id, detail,
                    created_at, updated_at)
               VALUES (%(run_id)s, %(dispute)s, %(status)s, %(summary)s,
                       1200, 300, 1500, 0.0042, 3,
                       %(confidence)s, %(proposed)s, %(approval_id)s,
                       CAST(%(detail)s AS JSON),
                       now() - make_interval(secs => %(age)s),
                       now() - make_interval(secs => %(age)s)
                         + make_interval(secs => %(dur)s))""",
            {"run_id": run_id, "dispute": "PP-D-88001", "status": status,
             "summary": f"{status} summary", "confidence": confidence,
             "proposed": proposed, "approval_id": approval_id,
             "detail": json.dumps(detail), "age": age_seconds,
             "dur": duration_seconds},
        )


def _seed_webhook_rows():
    with session_scope() as s:
        for i in range(2):
            s.add(WebhookEvent(
                external_event_id=f"WH-DASH-{i}",
                event_type="CHECKOUT.DISPUTE.CREATED",
                payload={"i": i}, verification="shared_secret"))
        done = WebhookOutbox(job_type="dispute_agent",
                             dedup_key="dispute:PP-D-DASH1",
                             payload={"dispute_id": "PP-D-DASH1"},
                             status="processed", attempts=1)
        dead = WebhookOutbox(job_type="dispute_agent",
                             dedup_key="dispute:PP-D-DASH2",
                             payload={"dispute_id": "PP-D-DASH2"},
                             status="dead", attempts=5, last_error=LONG_ERROR)
        s.add(done)
        s.add(dead)
        s.flush()
        done.processed_at = done.created_at
        dead.failed_at = dead.created_at


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/actions", "/audit", "/approvals", "/metrics", "/dead-letter",
])
def test_every_endpoint_requires_admin_key(client, path):
    assert client.get(BASE + path).status_code == 401
    assert client.get(BASE + path,
                      headers={"X-Admin-Key": "***"}).status_code == 401


# ---------------------------------------------------------------------------
# 1. Action feed
# ---------------------------------------------------------------------------

def test_actions_feed_newest_first_with_agent_names(client, decide, make_agent):
    agent_id, key = make_agent()
    d1 = decide(agent_id, req("25.00"))     # allowed
    d2 = decide(agent_id, req("150.00"))    # denied: per-txn cap is 100

    r = client.get(BASE + "/actions", headers=AH)

    assert r.status_code == 200
    actions = r.json()["actions"]
    assert [a["id"] for a in actions] == sorted(
        [a["id"] for a in actions], reverse=True)  # newest first
    by_id = {a["id"]: a for a in actions}
    a1, a2 = by_id[d1.action_id], by_id[d2.action_id]
    assert a1["outcome"] == "allowed" and a1["reason_code"] == "OK"
    assert a2["outcome"] == "denied"
    assert a2["reason_code"] == "PER_TXN_CAP_EXCEEDED"
    assert a1["amount"] == "25.00" and a1["currency"] == "USD"
    assert a1["agent_name"]  # join produced the name


# ---------------------------------------------------------------------------
# 2. Audit log + filters
# ---------------------------------------------------------------------------

def test_audit_filters(client, decide, make_agent):
    agent_a, _ = make_agent()
    agent_b, _ = make_agent()
    decide(agent_a, req("25.00"))            # allowed
    decide(agent_a, req("150.00"))           # denied
    decide(agent_b, req("10.00"))            # allowed, other agent

    all_rows = client.get(BASE + "/audit", headers=AH).json()["audit"]
    assert len(all_rows) >= 3

    denied = client.get(BASE + "/audit", headers=AH,
                        params={"decision": "denied"}).json()["audit"]
    assert denied and all(r["decision"] == "denied" for r in denied)

    only_b = client.get(BASE + "/audit", headers=AH,
                        params={"agent_id": agent_b}).json()["audit"]
    assert only_b and all(r["agent_id"] == agent_b for r in only_b)

    # action_type joins through action_id
    refunds = client.get(BASE + "/audit", headers=AH,
                         params={"action_type": "refund"}).json()["audit"]
    assert refunds and all(r["action_type"] == "refund" for r in refunds)
    empty = client.get(BASE + "/audit", headers=AH,
                       params={"action_type": "capture"}).json()["audit"]
    assert empty == []

    # time window: nothing happened in the future or before the epoch+1h
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert client.get(BASE + "/audit", headers=AH,
                      params={"since": future}).json()["audit"] == []
    assert client.get(BASE + "/audit", headers=AH,
                      params={"until": past}).json()["audit"] == []
    assert len(client.get(BASE + "/audit", headers=AH,
                          params={"since": past}).json()["audit"]) >= 3


# ---------------------------------------------------------------------------
# 3. Approvals + cross-DB reasoning join
# ---------------------------------------------------------------------------

def test_pending_approvals_carry_agent_reasoning(client, decide, make_agent):
    _, approval_id = _make_pending(decide, make_agent)
    _seed_agent_run(
        "webhook-PP-D-88001", approval_id, "pending_approval",
        reasoning=("Tracking shows no movement for 30 days and the buyer's "
                   "address never received the parcel; refund is within caps."),
        confidence=0.82, proposed="issue_refund")

    r = client.get(BASE + "/approvals", headers=AH)

    assert r.status_code == 200
    approvals = r.json()["approvals"]
    assert len(approvals) == 1
    ap = approvals[0]
    assert ap["approval_id"] == approval_id
    assert ap["status"] == "pending"
    assert ap["action"]["amount"] == "75.00"
    assert ap["expires_at"]  # TTL visible for the dashboard countdown
    agent = ap["agent"]
    assert agent is not None
    assert agent["run_id"] == "webhook-PP-D-88001"
    assert agent["dispute_id"] == "PP-D-88001"
    assert agent["confidence"] == 0.82
    assert agent["proposed_action"] == "issue_refund"
    # The reasoning and EVERY cited evidence item arrive uncut.
    assert "Tracking shows no movement for 30 days" in agent["reasoning"]
    assert len(agent["cited_evidence"]) == 2
    assert agent["cited_evidence"][0]["transaction_id"] == "05Y454980K6595141"


def test_approval_without_agent_row_has_null_agent(client, decide, make_agent):
    _, approval_id = _make_pending(decide, make_agent)

    ap = client.get(BASE + "/approvals", headers=AH).json()["approvals"][0]

    assert ap["approval_id"] == approval_id
    assert ap["agent"] is None  # no linked run — shown, not hidden


def test_approvals_status_filter(client, decide, make_agent):
    _, approval_id = _make_pending(decide, make_agent)
    r = client.post(f"/v1/admin/approvals/{approval_id}/decision", headers=AH,
                    json={"decision": "approved", "decided_by": "ops"})
    assert r.status_code == 200

    pending = client.get(BASE + "/approvals", headers=AH).json()["approvals"]
    assert pending == []  # default filter is pending
    approved = client.get(BASE + "/approvals", headers=AH,
                          params={"status": "approved"}).json()["approvals"]
    assert [a["approval_id"] for a in approved] == [approval_id]
    assert approved[0]["decided_by"] == "ops"
    everything = client.get(BASE + "/approvals", headers=AH,
                            params={"status": "all"}).json()["approvals"]
    assert [a["approval_id"] for a in everything] == [approval_id]


# ---------------------------------------------------------------------------
# 4. Metrics
# ---------------------------------------------------------------------------

def test_metrics_counts(client, decide, make_agent):
    agent_id, _ = make_agent(threshold="50.00")
    key = uuid4().hex
    decide(agent_id, req("25.00", key))     # allowed
    decide(agent_id, req("25.00", key))     # duplicate -> deduplicated
    decide(agent_id, req("150.00"))         # denied
    d = decide(agent_id, req("75.00"))      # pending approval
    assert d.outcome == "pending_approval"
    _seed_webhook_rows()
    _seed_agent_run("webhook-PP-D-88001", d.approval_id, "pending_approval",
                    confidence=0.7, proposed="issue_refund", age_seconds=30)
    _seed_agent_run("webhook-PP-D-88002", None, "refund_executed",
                    confidence=0.9, proposed="issue_refund",
                    age_seconds=200, duration_seconds=90)

    m = client.get(BASE + "/metrics", headers=AH).json()

    assert m["actions"]["total"] == 4
    assert m["actions"]["denied"] == 1
    assert m["actions"]["duplicates_absorbed"] == 1
    assert m["actions"]["by_outcome"]["allowed"] == 1
    assert m["actions"]["by_outcome"]["pending_approval"] == 1
    assert m["approvals"]["pending"] == 1
    assert m["webhooks"]["events_received"] == 2
    assert m["webhooks"]["jobs_by_status"] == {"processed": 1, "dead": 1}
    assert m["webhooks"]["dead_letter"] == 1
    agent = m["agent"]
    assert agent["runs_total"] == 2
    assert agent["awaiting_approval"] == 1
    assert agent["refunds_executed"] == 1
    assert agent["total_tokens"] == 3000          # 1500 per seeded run
    assert 85 <= agent["median_run_seconds"] <= 95  # only finished run counts


# ---------------------------------------------------------------------------
# 5. Dead letter — full error text, never truncated
# ---------------------------------------------------------------------------

def test_dead_letter_returns_full_error(client):
    _seed_webhook_rows()

    r = client.get(BASE + "/dead-letter", headers=AH)

    assert r.status_code == 200
    jobs = r.json()["dead_letter"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_type"] == "dispute_agent"
    assert job["attempts"] == 5
    assert job["last_error"] == LONG_ERROR  # whole text, byte for byte
    assert job["failed_at"] and job["created_at"]
    assert job["payload"] == {"dispute_id": "PP-D-DASH2"}


# ---------------------------------------------------------------------------
# Degradation: the agent DB being down must not take the dashboard with it
# ---------------------------------------------------------------------------

def test_agent_db_unreachable_degrades_not_fails(client, decide, make_agent,
                                                 monkeypatch):
    _, approval_id = _make_pending(decide, make_agent)
    # Port 9 (discard): nothing listens, connect fails fast.
    monkeypatch.setenv("AGENT_DATABASE_URL",
                       "postgresql://tollgate:tollgate@localhost:9/agent")

    ap = client.get(BASE + "/approvals", headers=AH)
    assert ap.status_code == 200
    row = ap.json()["approvals"][0]
    assert row["approval_id"] == approval_id  # gateway facts still served
    assert row["agent"] is None               # reasoning degraded to null

    m = client.get(BASE + "/metrics", headers=AH)
    assert m.status_code == 200
    assert m.json()["agent"] is None
    assert m.json()["actions"]["total"] >= 1
