"""Webhook endpoint tests: verification (both strategies, fail-closed),
structure validation, atomic event+job persistence, and the two duplicate
guards (event id unique, dispute dedup key unique).

Topology: FastAPI TestClient over the real app (lifespan runs, real
PostgreSQL via conftest). The PayPal verify API is served by a
MockTransport client swapped into app.state.paypal — the same injection
point the lifespan itself uses.
"""

import hashlib
import hmac as hmac_mod
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from gateway.db import session_scope
from gateway.models import WebhookEvent, WebhookOutbox
from gateway.paypal_client import PayPalClient

WEBHOOK_URL = "/webhooks/paypal"
SECRET = "test-webhook-secret"
WEBHOOK_ID = "WH-TEST-ID-123"

PAYPAL_SIG_HEADERS = {
    "paypal-auth-algo": "SHA256withRSA",
    "paypal-cert-url": "https://api.paypal.com/v1/notifications/certs/CERT-1",
    "paypal-transmission-id": "trans-1",
    "paypal-transmission-sig": "sig==",
    "paypal-transmission-time": "2026-11-09T10:00:00Z",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Both verification credentials unset by default — tests opt in."""
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "")
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "")


@pytest.fixture()
def client():
    from main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Payload + signing helpers
# ---------------------------------------------------------------------------

def _dispute_event(event_id: str | None = None,
                   dispute_id: str = "PP-D-56244",
                   event_type: str = "CHECKOUT.DISPUTE.CREATED") -> dict:
    """A realistic dispute webhook payload (sandbox resource shape)."""
    return {
        "id": event_id or f"WH-EVT-{uuid4().hex[:12].upper()}",
        "event_version": "1.0",
        "create_time": "2026-11-09T10:00:00.000Z",
        "event_type": event_type,
        "summary": "A customer initiated a chargeback.",
        "resource": {
            "dispute_id": dispute_id,
            "create_time": "2026-11-09T09:59:00.000Z",
            "update_time": "2026-11-09T09:59:30.000Z",
            "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
            "status": "WAITING_FOR_SELLER_RESPONSE",
            "dispute_amount": {"currency_code": "USD", "value": "10.99"},
            "disputed_transactions": [{
                "seller_transaction_id": "05Y454980K6595141",
                "seller_transaction_status": "PARTIALLY_REFUNDED",
            }],
        },
        "links": [],
    }


def _signed(event: dict, secret: str = SECRET) -> tuple[bytes, dict]:
    """(raw body, headers) with a valid HMAC signature over the raw bytes."""
    raw = json.dumps(event).encode()
    digest = hmac_mod.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {
        "Content-Type": "application/json",
        "X-Tollgate-Signature": f"sha256={digest}",
    }


def _enable_hmac(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", SECRET)


def _install_verifier(client, verify_calls: list | None = None,
                      status: str = "SUCCESS", api_fail: bool = False):
    """Swap app.state.paypal for a MockTransport client that answers the
    verify-webhook-signature call (and records request bodies)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(
                200, json={"access_token": "mock-token", "expires_in": 32400})
        if path == "/v1/notifications/verify-webhook-signature":
            if verify_calls is not None:
                verify_calls.append(json.loads(request.content))
            if api_fail:
                return httpx.Response(500, json={
                    "name": "INTERNAL_SERVER_ERROR",
                    "message": "verifier down", "debug_id": "mock-debug"})
            return httpx.Response(200, json={"verification_status": status})
        return httpx.Response(404, json={
            "name": "RESOURCE_NOT_FOUND", "message": "no", "debug_id": "x"})

    client.app.state.paypal = PayPalClient(
        client_id="mock", client_secret="mock",
        base_url="https://mock.paypal.local",
        transport=httpx.MockTransport(handler))


def _counts() -> tuple[int, int]:
    with session_scope() as s:
        events = s.execute(select(func.count()).select_from(WebhookEvent)).scalar_one()
        jobs = s.execute(select(func.count()).select_from(WebhookOutbox)).scalar_one()
    return events, jobs


# ---------------------------------------------------------------------------
# Shared-secret (HMAC) strategy
# ---------------------------------------------------------------------------

def test_hmac_valid_accepted_with_event_and_job(client, monkeypatch):
    _enable_hmac(monkeypatch)
    event = _dispute_event(event_id="WH-EVT-ACCEPTED-1")
    raw, headers = _signed(event)

    r = client.post(WEBHOOK_URL, content=raw, headers=headers)

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["job_created"] is True

    with session_scope() as s:
        row = s.execute(select(WebhookEvent).where(
            WebhookEvent.external_event_id == "WH-EVT-ACCEPTED-1")).scalar_one()
        assert row.verification == "shared_secret"
        assert row.event_type == "CHECKOUT.DISPUTE.CREATED"
        assert row.payload["resource"]["dispute_id"] == "PP-D-56244"
        job = s.execute(select(WebhookOutbox)).scalar_one()
    assert job.job_type == "dispute_agent"
    assert job.dedup_key == "dispute:PP-D-56244"
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.payload["external_event_id"] == "WH-EVT-ACCEPTED-1"


def test_hmac_invalid_signature_rejected(client, monkeypatch):
    _enable_hmac(monkeypatch)
    raw, headers = _signed(_dispute_event())
    headers["X-Tollgate-Signature"] = "sha256=" + "0" * 64  # tampered

    r = client.post(WEBHOOK_URL, content=raw, headers=headers)

    assert r.status_code == 401
    assert r.json()["detail"]["reason_code"] == "INVALID_SIGNATURE"
    assert _counts() == (0, 0)  # nothing persisted


def test_hmac_wrong_secret_rejected(client, monkeypatch):
    _enable_hmac(monkeypatch)
    raw, headers = _signed(_dispute_event(), secret="attacker" "-guess")

    r = client.post(WEBHOOK_URL, content=raw, headers=headers)

    assert r.status_code == 401
    assert _counts() == (0, 0)


def test_hmac_header_without_secret_configured_rejected(client):
    # WEBHOOK_SHARED_SECRET unset (autouse fixture): the header must NOT
    # silently downgrade to another strategy.
    raw, headers = _signed(_dispute_event())

    r = client.post(WEBHOOK_URL, content=raw, headers=headers)

    assert r.status_code == 401
    assert r.json()["detail"]["reason_code"] == "SHARED_SECRET_NOT_CONFIGURED"
    assert _counts() == (0, 0)


def test_no_verification_configured_fails_closed(client):
    r = client.post(WEBHOOK_URL, json=_dispute_event())

    assert r.status_code == 503
    assert r.json()["detail"]["reason_code"] == "WEBHOOK_VERIFICATION_UNCONFIGURED"
    assert _counts() == (0, 0)


# ---------------------------------------------------------------------------
# PayPal strategy
# ---------------------------------------------------------------------------

def _paypal_headers(**overrides) -> dict:
    return {"Content-Type": "application/json", **PAYPAL_SIG_HEADERS, **overrides}


def test_paypal_verification_success(client, monkeypatch):
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", WEBHOOK_ID)
    calls: list = []
    _install_verifier(client, verify_calls=calls)
    event = _dispute_event(event_id="WH-EVT-PAYPAL-1")

    r = client.post(WEBHOOK_URL, content=json.dumps(event).encode(),
                    headers=_paypal_headers())

    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    with session_scope() as s:
        row = s.execute(select(WebhookEvent)).scalar_one()
    assert row.verification == "paypal"
    # The verify call carried the configured webhook id and the event VERBATIM.
    assert len(calls) == 1
    assert calls[0]["webhook_id"] == WEBHOOK_ID
    assert calls[0]["webhook_event"] == event
    assert calls[0]["transmission_id"] == "trans-1"


def test_paypal_verification_failure_rejected(client, monkeypatch):
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", WEBHOOK_ID)
    _install_verifier(client, status="FAILURE")

    r = client.post(WEBHOOK_URL, content=json.dumps(_dispute_event()).encode(),
                    headers=_paypal_headers())

    assert r.status_code == 401
    assert r.json()["detail"]["reason_code"] == "INVALID_SIGNATURE"
    assert _counts() == (0, 0)


def test_paypal_missing_headers_rejected(client, monkeypatch):
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", WEBHOOK_ID)
    _install_verifier(client)
    partial = {"Content-Type": "application/json",
               "paypal-auth-algo": "SHA256withRSA"}  # four headers missing

    r = client.post(WEBHOOK_URL, content=json.dumps(_dispute_event()).encode(),
                    headers=partial)

    assert r.status_code == 401
    detail = r.json()["detail"]
    assert detail["reason_code"] == "MISSING_SIGNATURE_HEADERS"
    assert len(detail["headers"]) == 4
    assert _counts() == (0, 0)


def test_paypal_verifier_unavailable_fails_closed(client, monkeypatch):
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", WEBHOOK_ID)
    _install_verifier(client, api_fail=True)  # verify API returns 500

    r = client.post(WEBHOOK_URL, content=json.dumps(_dispute_event()).encode(),
                    headers=_paypal_headers())

    # 502 (non-2xx) so PayPal retries the delivery once the verifier recovers.
    assert r.status_code == 502
    assert r.json()["detail"]["reason_code"] == "VERIFICATION_UNAVAILABLE"
    assert _counts() == (0, 0)


# ---------------------------------------------------------------------------
# Structure validation
# ---------------------------------------------------------------------------

def test_malformed_json_rejected(client, monkeypatch):
    _enable_hmac(monkeypatch)
    raw = b"{not json"
    digest = hmac_mod.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()

    r = client.post(WEBHOOK_URL, content=raw, headers={
        "Content-Type": "application/json",
        "X-Tollgate-Signature": f"sha256={digest}"})

    assert r.status_code == 400
    assert r.json()["detail"]["reason_code"] == "MALFORMED_EVENT"
    assert _counts() == (0, 0)


@pytest.mark.parametrize("mutate", [
    lambda e: e.pop("id"),
    lambda e: e.pop("event_type"),
    lambda e: e.update(resource="not-an-object"),
    lambda e: e.update(id="   "),
])
def test_missing_required_fields_rejected(client, monkeypatch, mutate):
    _enable_hmac(monkeypatch)
    event = _dispute_event()
    mutate(event)
    raw, headers = _signed(event)

    r = client.post(WEBHOOK_URL, content=raw, headers=headers)

    assert r.status_code == 400
    assert r.json()["detail"]["reason_code"] == "MALFORMED_EVENT"
    assert _counts() == (0, 0)


def test_dispute_event_without_dispute_id_rejected(client, monkeypatch):
    _enable_hmac(monkeypatch)
    event = _dispute_event()
    del event["resource"]["dispute_id"]  # cannot dispatch or dedup without it
    raw, headers = _signed(event)

    r = client.post(WEBHOOK_URL, content=raw, headers=headers)

    assert r.status_code == 400
    assert _counts() == (0, 0)


# ---------------------------------------------------------------------------
# Duplicate guards
# ---------------------------------------------------------------------------

def test_duplicate_delivery_idempotent(client, monkeypatch):
    _enable_hmac(monkeypatch)
    event = _dispute_event(event_id="WH-EVT-DUP-1")
    raw, headers = _signed(event)

    first = client.post(WEBHOOK_URL, content=raw, headers=headers)
    second = client.post(WEBHOOK_URL, content=raw, headers=headers)

    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    assert _counts() == (1, 1)  # one event, one job — never two


def test_two_event_ids_same_dispute_enqueue_one_job(client, monkeypatch):
    _enable_hmac(monkeypatch)
    e1 = _dispute_event(event_id="WH-EVT-A", dispute_id="PP-D-77777")
    e2 = _dispute_event(event_id="WH-EVT-B", dispute_id="PP-D-77777",
                        event_type="CHECKOUT.DISPUTE.UPDATED")
    raw1, h1 = _signed(e1)
    raw2, h2 = _signed(e2)

    r1 = client.post(WEBHOOK_URL, content=raw1, headers=h1)
    r2 = client.post(WEBHOOK_URL, content=raw2, headers=h2)

    assert r1.status_code == 200 and r1.json()["job_created"] is True
    # The second event is NEW (different id) but its dispute already has a job.
    assert r2.status_code == 200 and r2.json()["status"] == "accepted"
    assert r2.json()["job_created"] is False
    events, jobs = _counts()
    assert (events, jobs) == (2, 1)


def test_non_dispute_event_recorded_without_job(client, monkeypatch):
    _enable_hmac(monkeypatch)
    event = {
        "id": "WH-EVT-CAP-1",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "05Y454980K6595141", "status": "COMPLETED",
                     "amount": {"currency_code": "USD", "value": "10.99"}},
    }
    raw, headers = _signed(event)

    r = client.post(WEBHOOK_URL, content=raw, headers=headers)

    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert r.json()["job_created"] is False
    assert _counts() == (1, 0)  # recorded for forensics, no agent dispatched
