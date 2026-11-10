"""Create a REAL sandbox dispute via PayPal's customer-disputes API.

Unlike scripts/simulate_dispute.py — which forges an HMAC dev webhook and
therefore can never pass production's fail-closed verification — this
script makes PayPal itself fire a genuinely signed
CUSTOMER.DISPUTE.CREATED delivery at whatever webhook URL is registered
on the sandbox app. Production included. That is what makes it the E2E
demo: real signature verification, real outbox job, real agent run.

Sandbox-only, like the endpoint itself.

Two modes:
  --check     read-only: fetch the capture and show status/amount so you
              can see what is disputable before filing.
  (default)   create the dispute and print the dispute id.

The PayPal-Auth-Assertion header is the UNSIGNED JWT shape PayPal
recommends for sandbox testing (developer.paypal.com, Disputes setup):
  header     {"alg": "none", "typ": "JWT"}
  payload    {"iss": CLIENT_ID, "email": BUYER_EMAIL}
  signature  ""            (parts base64url-encoded, joined with ".")
BUYER_EMAIL must be the sandbox personal account that paid the capture.

Run from the project root:
    uv run python scripts/demo_dispute.py --check --capture-id 05Y...
    uv run python scripts/demo_dispute.py --capture-id 05Y... \
        --amount 5.00 --buyer-email sb-...@personal.example.com
"""

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from config import get_settings  # noqa: E402
from gateway.paypal_client import PayPalClient  # noqa: E402


def _b64url(obj: dict) -> str:
    """base64url without padding — the JWT encoding PayPal's own docs use."""
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def build_auth_assertion(client_id: str, buyer_email: str) -> str:
    header = _b64url({"alg": "none", "typ": "JWT"})
    payload = _b64url({"iss": client_id, "email": buyer_email})
    return f"{header}.{payload}."  # empty signature segment


def check_capture(client: PayPalClient, capture_id: str) -> None:
    capture = client.get_capture(capture_id)
    amount = capture.get("amount", {})
    print(f"capture {capture_id}")
    print(f"  status      : {capture.get('status')}")
    print(f"  amount      : {amount.get('value')} {amount.get('currency_code')}")
    refunds = capture.get("refunds") or []
    if refunds:
        for r in refunds:
            ra = r.get("amount", {})
            print(f"  refund      : {ra.get('value')} {ra.get('currency_code')} "
                  f"({r.get('status')}) id={r.get('id')}")
    print("  (dispute_amount must not exceed amount minus refunds)")


def create_dispute(settings, capture_id: str, amount: str,
                   buyer_email: str, reason: str) -> None:
    client = PayPalClient(
        settings.paypal_client_id,
        settings.paypal_client_secret,
        settings.paypal_base_url,
    )
    token = client.get_access_token()
    assertion = build_auth_assertion(settings.paypal_client_id, buyer_email)

    payload = {
        "disputed_transactions": [{"buyer_transaction_id": capture_id}],
        "reason": reason,
        "dispute_amount": {"currency_code": "USD", "value": amount},
        "extensions": {
            "merchant_contacted": True,
            "merchant_contacted_outcome": "NO_RESPONSE",
            "merchandize_dispute_properties": {"issue_type": "PRODUCT"},
        },
    }
    url = settings.paypal_base_url.rstrip("/") + "/v1/customer/disputes"
    resp = httpx.post(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "PayPal-Auth-Assertion": assertion,
        },
        # multipart/form-data with the JSON part, exactly like the docs'
        # curl -F 'input={...};type=application/json'
        files={"input": (None, json.dumps(payload), "application/json")},
        timeout=60,
    )
    print(f"POST /v1/customer/disputes -> HTTP {resp.status_code}")
    location = resp.headers.get("location", "")
    if location:
        print(f"  location    : {location}")
        print(f"  dispute id  : {location.rstrip('/').rsplit('/', 1)[-1]}")
    try:
        print("  response    :", json.dumps(resp.json(), indent=2)[:800])
    except ValueError:
        print("  body        :", resp.text[:400])
    if resp.status_code >= 300:
        raise SystemExit("Dispute creation failed — see response above.")
    print("\nPayPal now fires CUSTOMER.DISPUTE.CREATED at every webhook "
          "registered on this app.\nWatch: production logs "
          "('Webhook accepted ... verified via paypal, job=True') and the "
          "dashboard.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--capture-id", required=True,
                        help="the buyer_transaction_id to dispute")
    parser.add_argument("--amount", help="dispute amount, e.g. 5.00 "
                                         "(required unless --check)")
    parser.add_argument("--buyer-email",
                        help="sandbox personal account email that paid the "
                             "capture (required unless --check)")
    parser.add_argument("--reason", default="MERCHANDISE_OR_SERVICE_NOT_RECEIVED")
    parser.add_argument("--check", action="store_true",
                        help="read-only: show capture status/amount/refunds")
    args = parser.parse_args()

    settings = get_settings()
    if settings.paypal_mode != "sandbox":
        raise SystemExit("Refusing to run: PAYPAL_MODE is not 'sandbox'.")

    client = PayPalClient(
        settings.paypal_client_id,
        settings.paypal_client_secret,
        settings.paypal_base_url,
    )
    if args.check:
        check_capture(client, args.capture_id)
        return
    if not args.amount or not args.buyer_email:
        raise SystemExit("--amount and --buyer-email are required to create "
                         "a dispute (or use --check).")
    create_dispute(settings, args.capture_id, args.amount,
                   args.buyer_email, args.reason)


if __name__ == "__main__":
    main()
