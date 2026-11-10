"""Simulate a PayPal dispute webhook against the REAL /webhooks/paypal endpoint.

PayPal cannot sign locally generated events (and its sandbox cannot reach a
local URL), so this script signs the payload with the dev shared-secret
strategy: HMAC-SHA256 over the exact raw bytes, sent as
'X-Tollgate-Signature: sha256=<hex>'. The endpoint, verification gate,
structure validation, and persistence path are the production ones — only
the crypto strategy differs (gateway/webhooks.py module docstring).

Requires WEBHOOK_SHARED_SECRET in the environment, matching the gateway's.

Usage:
    uv run python scripts/simulate_dispute.py [--url URL] [--dispute-id ID]
        [--event-id ID] [--capture-id ID] [--amount 10.99] [--currency USD]
        [--reason REASON] [--event-type CUSTOMER.DISPUTE.CREATED]

Defaults generate a fresh dispute/event id each run. Pass explicit ids to
replay: the SAME --event-id twice exercises the duplicate-delivery guard;
a new --event-id with the same --dispute-id exercises the job dedup guard.

Note: the simulated dispute does not exist in the PayPal sandbox, so a
dispatched agent run will fail evidence gathering with a real
RESOURCE_NOT_FOUND (the fail-fast path). Point --capture-id at a real
sandbox capture to keep the transaction lookup side real.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

DEFAULT_URL = "http://localhost:8000/webhooks/paypal"
# The real sandbox capture used across this repo's demos (10.99 USD,
# PARTIALLY_REFUNDED) — keeps get_transaction lookups on the agent side real.
DEFAULT_CAPTURE_ID = "05Y454980K6595141"


def _rfc3339(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_payload(
    *,
    dispute_id: str,
    event_id: str,
    capture_id: str = DEFAULT_CAPTURE_ID,
    amount: str = "10.99",
    currency: str = "USD",
    reason: str = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
    event_type: str = "CUSTOMER.DISPUTE.CREATED",
    now: datetime | None = None,
) -> dict:
    """A realistic dispute webhook body, shaped like the sandbox's actual
    CUSTOMER.DISPUTE.CREATED deliveries (resource v2.0)."""
    now = now or datetime.now(timezone.utc)
    created = now - timedelta(minutes=5)
    evidence_due = now + timedelta(days=10)
    return {
        "id": event_id,
        "event_version": "1.0",
        "create_time": _rfc3339(now),
        "resource_type": "dispute",
        "resource_version": "2.0",
        "event_type": event_type,
        "summary": "A customer initiated a chargeback.",
        "resource": {
            "dispute_id": dispute_id,
            "create_time": _rfc3339(created),
            "update_time": _rfc3339(now),
            "disputed_transactions": [{
                "seller_transaction_id": capture_id,
                "seller_transaction_status": "PARTIALLY_REFUNDED",
                "seller_transaction_update_time": _rfc3339(created),
            }],
            "reason": reason,
            "message_id": f"MSG-{uuid4().hex[:12].upper()}",
            "status": "WAITING_FOR_SELLER_RESPONSE",
            "dispute_amount": {"currency_code": currency, "value": amount},
            "metadata": {
                "merchant_note": "Shipped with tracking; buyer claims non-delivery.",
            },
            "evidence_due_by": _rfc3339(evidence_due),
            "reply_by_date": _rfc3339(evidence_due),
            "seller_response_due_date": _rfc3339(evidence_due),
            "links": [{
                "href": f"https://api.sandbox.paypal.com/v1/customer/disputes/{dispute_id}",
                "rel": "self", "method": "GET",
            }],
        },
        "links": [{
            "href": f"https://api.sandbox.paypal.com/v1/notifications/webhooks-events/{event_id}",
            "rel": "self", "method": "GET",
        }],
    }


def sign(raw: bytes, secret: str) -> str:
    """The header value the endpoint's shared-secret verifier expects."""
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def post_event(url: str, payload: dict, secret: str,
               timeout: float = 10.0) -> httpx.Response:
    """Serialize ONCE, sign those exact bytes, post those exact bytes."""
    raw = json.dumps(payload).encode()
    return httpx.post(
        url, content=raw,
        headers={"Content-Type": "application/json",
                 "X-Tollgate-Signature": sign(raw, secret)},
        timeout=timeout,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--dispute-id", default=f"PP-D-{uuid4().int % 100000}")
    parser.add_argument("--event-id", default=f"WH-SIM-{uuid4().hex[:20].upper()}")
    parser.add_argument("--capture-id", default=DEFAULT_CAPTURE_ID)
    parser.add_argument("--amount", default="10.99")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--reason", default="MERCHANDISE_OR_SERVICE_NOT_RECEIVED")
    parser.add_argument("--event-type", default="CUSTOMER.DISPUTE.CREATED")
    args = parser.parse_args()

    secret = os.environ.get("WEBHOOK_SHARED_SECRET", "").strip()
    if not secret:
        raise SystemExit(
            "WEBHOOK_SHARED_SECRET must be set (same value the gateway uses).")

    payload = build_payload(
        dispute_id=args.dispute_id, event_id=args.event_id,
        capture_id=args.capture_id, amount=args.amount,
        currency=args.currency, reason=args.reason, event_type=args.event_type,
    )
    response = post_event(args.url, payload, secret)

    print(f"POST {args.url}")
    print(f"  event_id:   {payload['id']}")
    print(f"  dispute_id: {payload['resource']['dispute_id']}")
    print(f"  event_type: {payload['event_type']}")
    print(f"-> {response.status_code} {response.text}")
    if response.status_code != 200:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
