"""Admin approval-decision tests: the dashboard's human path.

POST /v1/admin/approvals/{id}/decision must:
- reuse the SAME state machine as the agent path (row lock, transition
  validation, audit + event + domain-outbox rows),
- enqueue the resume_agent job in the SAME transaction (a decision can
  never be recorded without the paused agent being resumed, and vice
  versa — proven by forcing the enqueue to fail and watching the whole
  decision roll back),
- reject a second decision with 409 (transition guard) without a second
  job (dedup key),
- be gated by X-Admin-Key like every admin route.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from gateway.db import session_scope
from gateway.models import Approval, AuditLog, WebhookOutbox
from gateway.policy_engine import ActionRequest

ADMIN_KEY = "tga_" + "test_" + "e" * 40
AH = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture(autouse=True)
def admin_env(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)


@pytest.fixture()
def client():
    from main import app

    with TestClient(app) as c:
        yield c


def _make_pending(decide, make_agent, amount: str = "75.00") -> int:
    """A real pending approval through the real policy engine (threshold 50)."""
    agent_id, _ = make_agent(threshold="50.00")
    d = decide(agent_id, ActionRequest(
        action_type="refund", amount=Decimal(amount),
        currency="USD", idempotency_key=uuid4().hex))
    assert d.outcome == "pending_approval"
    return d.approval_id


def _decision_url(approval_id: int) -> str:
    return f"/v1/admin/approvals/{approval_id}/decision"


def _jobs() -> list[WebhookOutbox]:
    with session_scope() as s:
        return list(s.execute(select(WebhookOutbox)).scalars())


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_approved_decision_flips_approval_and_enqueues_resume(client, decide,
                                                              make_agent):
    approval_id = _make_pending(decide, make_agent)

    r = client.post(_decision_url(approval_id), headers=AH,
                    json={"decision": "approved", "decided_by": "ops-human"})

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved"
    assert body["reason_code"] == "APPROVED_BY_OPERATOR"
    assert body["decided_by"] == "ops-human"
    assert body["resume_job_id"] is not None

    with session_scope() as s:
        approval = s.get(Approval, approval_id)
        assert approval.status == "approved"
        # The state machine reuse is visible: the decision was audited.
        audit = s.execute(select(AuditLog).where(
            AuditLog.decision == "approved")).scalars().all()
    assert len(audit) == 1
    assert audit[0].reason_code == "APPROVED_BY_OPERATOR"

    jobs = _jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_type == "resume_agent"
    assert job.dedup_key == f"resume:{approval_id}"
    assert job.status == "pending"
    assert job.payload == {"approval_id": approval_id, "approved": True,
                           "decided_by": "ops-human"}


def test_rejected_decision_enqueues_resume_with_approved_false(client, decide,
                                                               make_agent):
    approval_id = _make_pending(decide, make_agent)

    r = client.post(_decision_url(approval_id), headers=AH,
                    json={"decision": "rejected", "decided_by": "ops-human"})

    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    job = _jobs()[0]
    assert job.payload["approved"] is False  # the agent wakes and stands down


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_requires_admin_key(client, decide, make_agent):
    approval_id = _make_pending(decide, make_agent)

    r = client.post(_decision_url(approval_id),
                    json={"decision": "approved", "decided_by": "x"})
    assert r.status_code == 401
    r2 = client.post(_decision_url(approval_id),
                     headers={"X-Admin-Key": "***"},
                     json={"decision": "approved", "decided_by": "x"})
    assert r2.status_code == 401
    # Nothing happened.
    with session_scope() as s:
        assert s.get(Approval, approval_id).status == "pending"
    assert _jobs() == []


def test_disabled_without_admin_env(client, decide, make_agent, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "")
    approval_id = _make_pending(decide, make_agent)

    r = client.post(_decision_url(approval_id), headers=AH,
                    json={"decision": "approved", "decided_by": "x"})

    assert r.status_code == 503
    assert r.json()["detail"]["reason_code"] == "ADMIN_DISABLED"


def test_unknown_approval_404(client):
    r = client.post(_decision_url(424242), headers=AH,
                    json={"decision": "approved", "decided_by": "x"})
    assert r.status_code == 404
    assert r.json()["detail"]["reason_code"] == "APPROVAL_NOT_FOUND"


def test_second_decision_conflicts_without_second_job(client, decide,
                                                      make_agent):
    approval_id = _make_pending(decide, make_agent)
    first = client.post(_decision_url(approval_id), headers=AH,
                        json={"decision": "approved", "decided_by": "one"})
    assert first.status_code == 200

    second = client.post(_decision_url(approval_id), headers=AH,
                         json={"decision": "rejected", "decided_by": "two"})

    assert second.status_code == 409
    assert second.json()["detail"]["reason_code"] == "INVALID_TRANSITION"
    assert len(_jobs()) == 1  # one decision, one resume job — forever


def test_invalid_decision_value_rejected_by_schema(client, decide, make_agent):
    approval_id = _make_pending(decide, make_agent)
    r = client.post(_decision_url(approval_id), headers=AH,
                    json={"decision": "maybe", "decided_by": "x"})
    assert r.status_code == 422  # pydantic Literal


# ---------------------------------------------------------------------------
# Atomicity: decision + resume job commit together or not at all
# ---------------------------------------------------------------------------

def test_enqueue_failure_rolls_back_the_decision(client, decide, make_agent,
                                                 monkeypatch):
    approval_id = _make_pending(decide, make_agent)

    def broken_enqueue(*args, **kwargs):
        raise RuntimeError("outbox insert exploded")

    monkeypatch.setattr("gateway.admin.enqueue_job", broken_enqueue)

    with pytest.raises(RuntimeError):
        client.post(_decision_url(approval_id), headers=AH,
                    json={"decision": "approved", "decided_by": "ops-human"})

    # The decision rolled back WITH the failed job: approval still pending,
    # no job row, no audit row claiming a decision that did not happen.
    with session_scope() as s:
        assert s.get(Approval, approval_id).status == "pending"
        assert s.execute(select(AuditLog).where(
            AuditLog.decision == "approved")).scalars().all() == []
    assert _jobs() == []
