"""API-level tests: auth, strict validation, rate limiting, ownership.

These run the real FastAPI app (TestClient triggers the lifespan) against
the migrated test database — the full stack: header extraction → SHA-256
lookup → Redis rate limit → Pydantic strict validation → policy engine →
committed audit rows.
"""

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from gateway.db import session_scope
from gateway.models import AuditLog


@pytest.fixture()
def client():
    from main import app

    with TestClient(app) as c:
        yield c


def _key(agent_tuple) -> dict:
    _, plaintext = agent_tuple
    return {"X-API-Key": plaintext}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def test_missing_key_401(client):
    r = client.post("/v1/agent-actions", json={
        "action_type": "refund", "amount": "1.00", "idempotency_key": uuid4().hex})
    assert r.status_code == 401
    assert r.json()["detail"]["reason_code"] == "MISSING_API_KEY"


def test_invalid_key_401_and_audited(client):
    r = client.post("/v1/agent-actions",
                    headers={"X-API-Key": "tg_" + "0" * 48},
                    json={"action_type": "refund", "amount": "1.00",
                          "idempotency_key": uuid4().hex})
    assert r.status_code == 401
    assert r.json()["detail"]["reason_code"] == "INVALID_API_KEY"
    with session_scope() as s:
        n = s.execute(select(func.count()).select_from(AuditLog).where(
            AuditLog.decision == "auth_failed",
            AuditLog.reason_code == "INVALID_API_KEY")).scalar_one()
    assert n >= 1


def test_bearer_auth_also_accepted(client, make_agent):
    _, key = make_agent()
    r = client.post("/v1/agent-actions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"action_type": "refund", "amount": "1.00",
                          "idempotency_key": uuid4().hex})
    assert r.status_code == 200
    assert r.json()["outcome"] == "allowed"


def test_inactive_agent_403(client, make_agent):
    _, key = make_agent(active=False)
    r = client.post("/v1/agent-actions", headers=_key((0, key)),
                    json={"action_type": "refund", "amount": "1.00",
                          "idempotency_key": uuid4().hex})
    assert r.status_code == 403
    assert r.json()["detail"]["reason_code"] == "AGENT_INACTIVE"


# ---------------------------------------------------------------------------
# Strict schema validation
# ---------------------------------------------------------------------------

def test_float_amount_rejected(client, make_agent):
    h = _key(make_agent())
    r = client.post("/v1/agent-actions", headers=h, json={
        "action_type": "refund", "amount": 10.5, "idempotency_key": uuid4().hex})
    assert r.status_code == 422


def test_extra_field_rejected(client, make_agent):
    h = _key(make_agent())
    r = client.post("/v1/agent-actions", headers=h, json={
        "action_type": "refund", "amount": "1.00",
        "idempotency_key": uuid4().hex, "surprise": True})
    assert r.status_code == 422


def test_malformed_amount_rejected(client, make_agent):
    h = _key(make_agent())
    for bad in ["abc", "-5.00", "1.234"]:  # garbage / negative / 3 decimals
        r = client.post("/v1/agent-actions", headers=h, json={
            "action_type": "refund", "amount": bad, "idempotency_key": uuid4().hex})
        assert r.status_code == 422, bad


def test_bad_action_type_pattern_rejected(client, make_agent):
    h = _key(make_agent())
    r = client.post("/v1/agent-actions", headers=h, json={
        "action_type": "Refund!", "amount": "1.00", "idempotency_key": uuid4().hex})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Decisions and ownership through the API
# ---------------------------------------------------------------------------

def test_dedup_via_api(client, make_agent):
    h = _key(make_agent())
    key = uuid4().hex
    body = {"action_type": "refund", "amount": "10.00", "idempotency_key": key}
    r1 = client.post("/v1/agent-actions", headers=h, json=body)
    r2 = client.post("/v1/agent-actions", headers=h, json=body)
    assert r1.json()["outcome"] == "allowed"
    j2 = r2.json()
    assert j2["outcome"] == "deduplicated"
    assert j2["duplicate_of_action_id"] == r1.json()["action_id"]


def test_cross_agent_action_is_404(client, make_agent):
    h1 = _key(make_agent())
    h2 = _key(make_agent())
    r = client.post("/v1/agent-actions", headers=h1, json={
        "action_type": "refund", "amount": "1.00", "idempotency_key": uuid4().hex})
    action_id = r.json()["action_id"]
    # Agent 1 sees its own action...
    assert client.get(f"/v1/actions/{action_id}", headers=h1).status_code == 200
    # ...agent 2 gets 404 for the same id (foreign probes must not confirm
    # the resource exists).
    assert client.get(f"/v1/actions/{action_id}", headers=h2).status_code == 404


# ---------------------------------------------------------------------------
# Rate limiting (Redis)
# ---------------------------------------------------------------------------

def test_per_agent_rate_limit_429(client, make_agent, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_AGENT_PER_MIN", "5")
    h = _key(make_agent())
    statuses = []
    for _ in range(7):
        r = client.post("/v1/agent-actions", headers=h, json={
            "action_type": "refund", "amount": "1.00", "idempotency_key": uuid4().hex})
        statuses.append(r.status_code)
    assert statuses[:5] == [200] * 5
    assert statuses[5] == 429
    # Retry-After header tells the client when the window rolls over.
    assert 0 < int(client.post("/v1/agent-actions", headers=h, json={
        "action_type": "refund", "amount": "1.00",
        "idempotency_key": uuid4().hex}).headers.get("Retry-After", "0")) <= 60
    # The rejection is audited against the agent.
    with session_scope() as s:
        n = s.execute(select(func.count()).select_from(AuditLog).where(
            AuditLog.decision == "rate_limited")).scalar_one()
    assert n >= 1
