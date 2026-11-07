"""Admin provisioning tests: agent creation, key issuance, rotation, revocation.

The headline test is test_newly_created_key_works_on_running_server: it
proves the property the admin API exists for — a key issued through HTTP on
a LIVE process is usable on the very next request, with no restart, because
auth reads agent_keys from PostgreSQL per request and nothing is cached
in-process.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from gateway.db import session_scope
from gateway.models import AuditLog

ADMIN_KEY = "tga_test_" + "f" * 40
AH = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture()
def client():
    from main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def admin_env(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)


def _create_body(name: str | None = None, **overrides) -> dict:
    body = {
        "name": name or f"acme-{uuid4().hex[:8]}",
        "allowed_actions": ["refund"],
        "per_txn_cap": "25.00",
        "rolling_24h_cap": "100.00",
        "approval_threshold": "20.00",
    }
    body.update(overrides)
    return body


def _action_call(client, api_key: str):
    return client.post(
        "/v1/agent-actions",
        headers={"X-API-Key": api_key},
        json={"action_type": "refund", "amount": "10.00",
              "idempotency_key": uuid4().hex},
    )


# ---------------------------------------------------------------------------
# Guard: the admin credential itself
# ---------------------------------------------------------------------------

def test_admin_disabled_without_env_key(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "")  # unset/empty → fail-safe 503
    r = client.post("/v1/admin/agents", headers=AH, json=_create_body())
    assert r.status_code == 503
    assert r.json()["detail"]["reason_code"] == "ADMIN_DISABLED"


def test_wrong_admin_key_401_and_audited(client):
    r = client.post("/v1/admin/agents",
                    headers={"X-Admin-Key": "tga_wrong"},
                    json=_create_body())
    assert r.status_code == 401
    assert r.json()["detail"]["reason_code"] == "INVALID_ADMIN_KEY"
    with session_scope() as s:
        n = s.execute(select(func.count()).select_from(AuditLog).where(
            AuditLog.decision == "auth_failed",
            AuditLog.reason_code == "INVALID_ADMIN_KEY")).scalar_one()
    assert n >= 1


def test_agent_key_is_not_an_admin_key(client, make_agent):
    _, agent_key = make_agent()
    r = client.post("/v1/admin/agents",
                    headers={"X-Admin-Key": agent_key},
                    json=_create_body())
    assert r.status_code == 401  # separation of duties holds


# ---------------------------------------------------------------------------
# Creation — and the live-process property
# ---------------------------------------------------------------------------

def test_newly_created_key_works_on_running_server(client):
    """Issue a key over HTTP on the live app → use it on the NEXT request.
    No restart, no reload: auth is a per-request indexed DB lookup."""
    r = client.post("/v1/admin/agents", headers=AH, json=_create_body())
    assert r.status_code == 200
    created = r.json()
    assert created["api_key"].startswith("tg_")
    assert created["key_prefix"] == created["api_key"][:11]

    immediately = _action_call(client, created["api_key"])
    assert immediately.status_code == 200
    assert immediately.json()["outcome"] == "allowed"


def test_duplicate_agent_name_409(client):
    name = f"dupe-{uuid4().hex[:8]}"
    assert client.post("/v1/admin/agents", headers=AH,
                       json=_create_body(name)).status_code == 200
    r = client.post("/v1/admin/agents", headers=AH, json=_create_body(name))
    assert r.status_code == 409
    assert r.json()["detail"]["reason_code"] == "AGENT_NAME_TAKEN"


def test_incoherent_policy_rejected_422(client):
    # threshold > per_txn_cap would make the approval path unreachable —
    # rejected at the edge, mirroring ck_policies_threshold_within_cap.
    r = client.post("/v1/admin/agents", headers=AH, json=_create_body(
        per_txn_cap="10.00", approval_threshold="50.00"))
    assert r.status_code == 422


def test_float_cap_rejected_422(client):
    r = client.post("/v1/admin/agents", headers=AH, json=_create_body(
        per_txn_cap=25.0))
    assert r.status_code == 422


def test_creation_is_audited(client):
    r = client.post("/v1/admin/agents", headers=AH, json=_create_body())
    agent_id = r.json()["agent_id"]
    with session_scope() as s:
        row = s.execute(select(AuditLog).where(
            AuditLog.agent_id == agent_id,
            AuditLog.decision == "agent_created")).scalar_one()
    assert row.detail["name"] is not None
    assert row.detail["key_prefix"] == r.json()["key_prefix"]


# ---------------------------------------------------------------------------
# Rotation: issue new key → both work → revoke old → only new works
# ---------------------------------------------------------------------------

def test_key_rotation_zero_downtime(client):
    created = client.post("/v1/admin/agents", headers=AH,
                          json=_create_body()).json()
    old_key, agent_id = created["api_key"], created["agent_id"]

    # Issue a second key; BOTH are valid during migration (no cutover gap).
    issued = client.post(f"/v1/admin/agents/{agent_id}/keys?label=rotation-1",
                         headers=AH).json()
    new_key = issued["api_key"]
    assert _action_call(client, old_key).status_code == 200
    assert _action_call(client, new_key).status_code == 200

    # Revoke the old key...
    rev = client.delete(f"/v1/admin/keys/{created['key_id']}", headers=AH)
    assert rev.status_code == 200
    assert rev.json()["already_revoked"] is False

    # ...it dies on the RUNNING server immediately; the new key is untouched.
    r_old = _action_call(client, old_key)
    assert r_old.status_code == 401
    assert r_old.json()["detail"]["reason_code"] == "API_KEY_REVOKED"
    assert _action_call(client, new_key).status_code == 200


def test_revoke_is_idempotent(client):
    created = client.post("/v1/admin/agents", headers=AH,
                          json=_create_body()).json()
    kid = created["key_id"]
    assert client.delete(f"/v1/admin/keys/{kid}", headers=AH).json()["already_revoked"] is False
    second = client.delete(f"/v1/admin/keys/{kid}", headers=AH)
    assert second.status_code == 200
    assert second.json()["already_revoked"] is True


def test_revoke_unknown_key_404(client):
    assert client.delete("/v1/admin/keys/999999", headers=AH).status_code == 404


def test_revoked_key_audit_trail(client):
    created = client.post("/v1/admin/agents", headers=AH,
                          json=_create_body()).json()
    client.delete(f"/v1/admin/keys/{created['key_id']}", headers=AH)
    _action_call(client, created["api_key"])  # rejected attempt, also audited
    with session_scope() as s:
        rows = s.execute(select(AuditLog).where(
            AuditLog.agent_id == created["agent_id"]).order_by(AuditLog.id)
        ).scalars().all()
    decisions = [(r.decision, r.reason_code) for r in rows]
    assert ("agent_created", "OK") in decisions
    assert ("key_revoked", "OK") in decisions
    assert ("auth_failed", "API_KEY_REVOKED") in decisions


# ---------------------------------------------------------------------------
# PATCH /v1/admin/agents/{id} — kill switch + policy updates
# ---------------------------------------------------------------------------

def _patch(client, agent_id, **fields):
    return client.patch(f"/v1/admin/agents/{agent_id}", headers=AH,
                        json={"actor": "ops-test", **fields})


def test_deactivate_locks_out_running_server(client):
    created = client.post("/v1/admin/agents", headers=AH,
                          json=_create_body()).json()
    aid, key = created["agent_id"], created["api_key"]
    assert _action_call(client, key).status_code == 200

    r = _patch(client, aid, is_active=False)
    assert r.status_code == 200
    assert r.json()["is_active"] is False

    # Dies on the RUNNING server: auth reads is_active per request.
    blocked = _action_call(client, key)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["reason_code"] == "AGENT_INACTIVE"

    # Re-enabling through the same endpoint works immediately too.
    assert _patch(client, aid, is_active=True).json()["is_active"] is True
    assert _action_call(client, key).status_code == 200


def test_policy_update_takes_effect_immediately(client):
    created = client.post("/v1/admin/agents", headers=AH,
                          json=_create_body()).json()
    aid, key = created["agent_id"], created["api_key"]

    # Rolling cap below one $10 action → the NEXT call is denied.
    r = _patch(client, aid, rolling_24h_cap="5.00")
    assert r.json()["policy"]["rolling_24h_cap"] == "5.00"
    denied = _action_call(client, key)
    assert denied.status_code == 200  # the decision IS the payload
    assert denied.json()["outcome"] == "denied"
    assert denied.json()["reason_code"] == "ROLLING_24H_CAP_EXCEEDED"

    # Narrowed allowlist → refund is no longer this agent's business
    # (cap restored so the denial we observe is the allowlist one).
    _patch(client, aid, rolling_24h_cap="100.00",
           allowed_actions=["create_order"])
    denied = _action_call(client, key)
    assert denied.json()["reason_code"] == "ACTION_NOT_ALLOWED"


def test_invariant_checked_against_effective_values(client):
    created = client.post("/v1/admin/agents", headers=AH,
                          json=_create_body()).json()
    aid = created["agent_id"]

    # Shrinking per_txn below the EXISTING threshold is caught...
    r = _patch(client, aid, per_txn_cap="5.00")
    assert r.status_code == 422
    assert r.json()["detail"]["reason_code"] == "POLICY_INVARIANT_VIOLATED"

    # ...unless the same request restores coherence (fields merge, they are
    # not applied one endpoint-at-a-time).
    r = _patch(client, aid, per_txn_cap="5.00", approval_threshold="4.00")
    assert r.status_code == 200
    assert r.json()["policy"]["per_txn_cap"] == "5.00"
    assert r.json()["policy"]["approval_threshold"] == "4.00"


def test_partial_patch_leaves_other_fields_untouched(client):
    created = client.post("/v1/admin/agents", headers=AH,
                          json=_create_body()).json()
    before = client.get(f"/v1/admin/agents/{created['agent_id']}",
                        headers=AH).json()["policy"]
    after = _patch(client, created["agent_id"], is_active=False).json()
    assert after["policy"] == before
    assert after["is_active"] is False


def test_patch_validation_edges(client):
    created = client.post("/v1/admin/agents", headers=AH,
                          json=_create_body()).json()
    aid = created["agent_id"]

    # Actor only → nothing to update.
    r = client.patch(f"/v1/admin/agents/{aid}", headers=AH,
                     json={"actor": "ops-test"})
    assert r.status_code == 422
    assert r.json()["detail"]["reason_code"] == "NOTHING_TO_UPDATE"

    # Explicit null: PATCH updates fields, it never removes them.
    r = _patch(client, aid, is_active=None)
    assert r.status_code == 422
    assert r.json()["detail"]["reason_code"] == "NULL_NOT_ALLOWED"

    assert _patch(client, 999999, is_active=False).status_code == 404
    assert client.patch(f"/v1/admin/agents/{aid}", headers=AH,
                        json={"is_active": False}).status_code == 422  # actor
    assert _patch(client, aid, allowed_actions=[]).status_code == 422
    assert _patch(client, aid, bogus_field=1).status_code == 422

    # None of the rejected patches above left an audit trail.
    with session_scope() as s:
        n = s.execute(select(func.count()).select_from(AuditLog).where(
            AuditLog.agent_id == aid,
            AuditLog.decision == "agent_updated")).scalar_one()
    assert n == 0


def test_update_is_audited_with_diff(client):
    created = client.post("/v1/admin/agents", headers=AH,
                          json=_create_body()).json()
    aid = created["agent_id"]
    _patch(client, aid, is_active=False, rolling_24h_cap="50.00")
    with session_scope() as s:
        row = s.execute(select(AuditLog).where(
            AuditLog.agent_id == aid,
            AuditLog.decision == "agent_updated")).scalar_one()
    assert row.detail["actor"] == "ops-test"
    assert row.detail["changes"]["is_active"] == {"before": True, "after": False}
    assert row.detail["changes"]["rolling_24h_cap"] == {
        "before": "100.00", "after": "50.00"}


def test_agent_key_is_not_an_admin_key_for_updates(client, make_agent):
    # Separation of duties holds for the kill switch too.
    created = client.post("/v1/admin/agents", headers=AH,
                          json=_create_body()).json()
    _, other_key = make_agent()
    r = client.patch(f"/v1/admin/agents/{created['agent_id']}",
                     headers={"X-Admin-Key": other_key},
                     json={"actor": "sneaky", "is_active": False})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Ops views
# ---------------------------------------------------------------------------

def test_list_and_get_agents_show_prefixes_not_hashes(client):
    created = client.post("/v1/admin/agents", headers=AH,
                          json=_create_body()).json()
    lst = client.get("/v1/admin/agents", headers=AH).json()
    entry = next(a for a in lst if a["id"] == created["agent_id"])
    assert entry["policy"]["per_txn_cap"] == "25.00"
    key_view = entry["keys"][0]
    assert key_view["key_prefix"] == created["key_prefix"]
    assert key_view["revoked_at"] is None
    # Hashes must never leave the database.
    assert "key_hash" not in str(entry)

    detail = client.get(f"/v1/admin/agents/{created['agent_id']}", headers=AH)
    assert detail.status_code == 200
    assert detail.json()["name"] == created["name"]
    assert client.get("/v1/admin/agents/999999", headers=AH).status_code == 404
