"""POST /webhooks/paypal — the inbound webhook pipeline's front door.

Contract with PayPal:
- Answer 200 fast. Everything beyond persistence (starting the dispute
  agent) happens later, in the outbox worker. A slow consumer makes PayPal
  retry, and retries pile up.
- Accept duplicate deliveries without duplicating work: the UNIQUE
  constraint on webhook_events.external_event_id is the first guard, and
  the partial unique index on webhook_outbox(job_type, dedup_key) is the
  second (two DIFFERENT event ids about the SAME dispute enqueue one job).

Signature verification, dual strategy (config.py):
- 'paypal' (production): PayPal signs every delivery with its private key.
  We forward the five PAYPAL-* transmission headers, the raw event body
  VERBATIM, and PAYPAL_WEBHOOK_ID to PayPal's verify-webhook-signature
  API, and trust only its SUCCESS. If the verifier is unreachable we fail
  CLOSED (502) — an unverifiable delivery is not an accepted delivery,
  and PayPal retries on non-2xx.
- 'shared_secret' (dev/simulation): scripts/simulate_dispute.py signs the
  raw body with HMAC-SHA256 under WEBHOOK_SHARED_SECRET and sends
  'X-Tollgate-Signature: sha256=<hex>'. PayPal cannot sign locally
  simulated events, so this is the only way the simulator can exercise
  the SAME endpoint, parsing, and persistence path as production — only
  the crypto check differs. Selection is by header presence: an
  X-Tollgate-Signature header uses the HMAC strategy; otherwise the
  PayPal strategy. Neither configured -> 503, fail closed.

The raw body matters: both strategies verify over the exact received
bytes, so the endpoint reads `await request.body()` before any parsing
and re-parses that same text for the verify API's webhook_event field.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import get_settings
from gateway.db import session_scope
from gateway.models import WebhookEvent, WebhookOutbox
from gateway.paypal_client import PayPalError

logger = logging.getLogger(__name__)

webhook_router = APIRouter()

# The five headers PayPal sends with every signed delivery.
_SIG_HEADERS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}
_HMAC_HEADER = "x-tollgate-signature"
_HMAC_PREFIX = "sha256="

# Only these event families start an agent run. Everything else is recorded
# (forensics + replayability) but enqueues nothing.
_DISPUTE_PREFIX = "CHECKOUT.DISPUTE."


def _reject(status_code: int, reason_code: str, **extra) -> HTTPException:
    """Error shape convention: detail = {"reason_code": ..., ...}."""
    return HTTPException(status_code=status_code,
                         detail={"reason_code": reason_code, **extra})


def _verify_hmac(raw: bytes, header_value: str, secret: str) -> bool:
    """Constant-time compare of 'sha256=<hex>' against HMAC(secret, raw)."""
    if not header_value.startswith(_HMAC_PREFIX):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value[len(_HMAC_PREFIX):])


def _verify(request: Request, raw: bytes, event: dict) -> str:
    """Run the applicable verification strategy. Returns the method name
    ('paypal' | 'shared_secret') or raises HTTPException (fail closed)."""
    settings = get_settings()
    hmac_header = request.headers.get(_HMAC_HEADER)

    if hmac_header is not None:
        if not settings.webhook_shared_secret:
            # Header present but strategy not configured: refuse rather than
            # silently downgrading to another strategy.
            raise _reject(401, "SHARED_SECRET_NOT_CONFIGURED")
        if not _verify_hmac(raw, hmac_header, settings.webhook_shared_secret):
            raise _reject(401, "INVALID_SIGNATURE", method="shared_secret")
        return "shared_secret"

    if not settings.paypal_webhook_id:
        raise _reject(503, "WEBHOOK_VERIFICATION_UNCONFIGURED")

    missing = [name for name in _SIG_HEADERS.values()
               if not request.headers.get(name)]
    if missing:
        raise _reject(401, "MISSING_SIGNATURE_HEADERS", headers=missing)

    # The client is the app-lifespan PayPalClient (app.state.paypal).
    client = request.app.state.paypal
    try:
        verified = client.verify_webhook_signature(
            auth_algo=request.headers[_SIG_HEADERS["auth_algo"]],
            cert_url=request.headers[_SIG_HEADERS["cert_url"]],
            transmission_id=request.headers[_SIG_HEADERS["transmission_id"]],
            transmission_sig=request.headers[_SIG_HEADERS["transmission_sig"]],
            transmission_time=request.headers[_SIG_HEADERS["transmission_time"]],
            webhook_id=settings.paypal_webhook_id,
            webhook_event=event,  # verbatim parse of the raw bytes
        )
    except PayPalError as exc:
        logger.error("Webhook verification API failed: %s", exc)
        # Fail closed. 502 (non-2xx) makes PayPal retry the delivery later.
        raise _reject(502, "VERIFICATION_UNAVAILABLE") from exc
    if not verified:
        raise _reject(401, "INVALID_SIGNATURE", method="paypal")
    return "paypal"


def _validate_structure(event: object) -> tuple[str, str, dict]:
    """Minimal shape check: id, event_type, resource object.

    Returns (external_event_id, event_type, resource). Deliberately lax
    beyond that — PayPal owns the schema, and over-validating here would
    reject new event versions we don't know yet. The dispute_id requirement
    below is the one domain-specific rule (we cannot dispatch or dedup
    without it).
    """
    if not isinstance(event, dict):
        raise _reject(400, "MALFORMED_EVENT", detail="body is not a JSON object")
    external_id = event.get("id")
    event_type = event.get("event_type")
    resource = event.get("resource")
    if not isinstance(external_id, str) or not external_id.strip():
        raise _reject(400, "MALFORMED_EVENT", detail="missing or empty 'id'")
    if not isinstance(event_type, str) or not event_type.strip():
        raise _reject(400, "MALFORMED_EVENT", detail="missing or empty 'event_type'")
    if not isinstance(resource, dict):
        raise _reject(400, "MALFORMED_EVENT", detail="missing or non-object 'resource'")
    if event_type.startswith(_DISPUTE_PREFIX):
        dispute_id = resource.get("dispute_id")
        if not isinstance(dispute_id, str) or not dispute_id.strip():
            raise _reject(400, "MALFORMED_EVENT",
                          detail="dispute event without resource.dispute_id")
    return external_id, event_type, resource


def _dispute_dedup_key(event_type: str, resource: dict) -> str | None:
    """Job dedup key: one agent run per dispute, no matter how many events
    (created, updated, redelivered under new ids) reference it."""
    if event_type.startswith(_DISPUTE_PREFIX):
        return f"dispute:{resource['dispute_id']}"
    return None


@webhook_router.post("/webhooks/paypal")
async def paypal_webhook(request: Request) -> dict:
    """Accept one verified PayPal webhook delivery. See module docstring."""
    raw = await request.body()  # verify over the EXACT bytes received
    try:
        event = json.loads(raw)
    except ValueError as exc:
        raise _reject(400, "MALFORMED_EVENT", detail="body is not valid JSON") from exc

    method = _verify(request, raw, event)
    external_id, event_type, resource = _validate_structure(event)

    # One transaction: event row + (maybe) job row commit atomically.
    # ON CONFLICT DO NOTHING turns both duplicate guards into no-ops instead
    # of exceptions: redelivery -> no event row -> no job; same dispute under
    # a new event id -> event row yes, second job no.
    with session_scope() as s:
        inserted_id = s.execute(
            pg_insert(WebhookEvent)
            .values(external_event_id=external_id, event_type=event_type,
                    payload=event, verification=method)
            .on_conflict_do_nothing(index_elements=["external_event_id"])
            .returning(WebhookEvent.id)
        ).scalar_one_or_none()

        if inserted_id is None:
            logger.info("Duplicate webhook delivery ignored: %s", external_id)
            return {"status": "duplicate", "event_id": external_id}

        dedup_key = _dispute_dedup_key(event_type, resource)
        job_created = False
        if dedup_key is not None:
            result = s.execute(
                pg_insert(WebhookOutbox)
                .values(job_type="dispute_agent", dedup_key=dedup_key,
                        payload={"external_event_id": external_id,
                                 "event_type": event_type,
                                 "dispute_id": resource["dispute_id"],
                                 "resource": resource})
                .on_conflict_do_nothing(
                    index_elements=["job_type", "dedup_key"],
                    index_where=WebhookOutbox.dedup_key.is_not(None),
                )
                .returning(WebhookOutbox.id)
            )
            job_created = result.scalar_one_or_none() is not None

    logger.info("Webhook accepted: %s (%s, verified via %s, job=%s)",
                external_id, event_type, method, job_created)
    return {"status": "accepted", "event_id": external_id,
            "event_type": event_type, "job_created": job_created}
