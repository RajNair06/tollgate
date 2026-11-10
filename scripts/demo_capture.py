"""Create a fresh sandbox capture for demos (e.g. the E2E video).

Flow:
1. Create an order (intent=CAPTURE) with the app credentials from .env.
2. Print the approval URL. YOU open it in a browser, log in as the
   sandbox BUYER (personal account), and approve. The script polls the
   order until its status becomes APPROVED.
3. Capture the order and print the capture ID plus the payer identity.

The payer email it prints is exactly the BUYER_EMAIL that
scripts/demo_dispute.py needs for its PayPal-Auth-Assertion JWT — the
dispute must be filed by the same buyer who paid.

Run from the project root:
    uv run python scripts/demo_capture.py --amount 30.00
"""

import argparse
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings  # noqa: E402
from gateway.paypal_client import (  # noqa: E402
    PayPalClient,
    get_approval_url,
    get_capture_id,
)

CURRENCY = "USD"
APPROVAL_TIMEOUT_SECONDS = 300  # 5 minutes to approve in the browser
POLL_INTERVAL_SECONDS = 5


def wait_for_approval(client: PayPalClient, order_id: str) -> dict:
    deadline = time.monotonic() + APPROVAL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        order = client.get_order(order_id)
        status = order.get("status")
        if status == "APPROVED":
            print(f"[2] Order {order_id} is APPROVED.")
            return order
        if status in ("VOIDED", "COMPLETED"):
            raise SystemExit(f"Unexpected order status: {status}")
        print(f"    status={status}, waiting...")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit("Timed out waiting for buyer approval. Re-run the script.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--amount", default="30.00",
                        help="order amount in USD (default: 30.00)")
    parser.add_argument("--description", default="Demo capture",
                        help="order description")
    args = parser.parse_args()

    settings = get_settings()
    if settings.paypal_mode != "sandbox":
        raise SystemExit("Refusing to run: PAYPAL_MODE is not 'sandbox'.")

    client = PayPalClient(
        settings.paypal_client_id,
        settings.paypal_client_secret,
        settings.paypal_base_url,
    )

    # -- 1. Create order ------------------------------------------------------
    order = client.create_order(
        Decimal(args.amount),
        CURRENCY,
        description=args.description,
        invoice_id=f"DEMO-{int(time.time())}",
    )
    order_id = order["id"]
    print(f"[1] Created order {order_id} | status={order['status']} "
          f"| amount={order['purchase_units'][0]['amount']['value']} "
          f"{order['purchase_units'][0]['amount']['currency_code']}")

    # -- 2. Buyer approval (manual, in browser) -------------------------------
    approval_url = get_approval_url(order)
    if not approval_url:
        raise SystemExit("No approval URL in order response.")
    print("\n" + "=" * 70)
    print("ACTION REQUIRED — approve this order as the sandbox BUYER:")
    print(f"\n  {approval_url}\n")
    print(f"Polling every {POLL_INTERVAL_SECONDS}s "
          f"(timeout {APPROVAL_TIMEOUT_SECONDS}s)...")
    print("=" * 70)
    wait_for_approval(client, order_id)

    # -- 3. Capture -----------------------------------------------------------
    captured = client.capture_order(order_id)
    capture = captured["purchase_units"][0]["payments"]["captures"][0]
    capture_id = get_capture_id(captured)
    payer = captured.get("payer") or {}
    print(f"[3] Captured order {order_id} | capture_id={capture_id} "
          f"| capture status={capture['status']}")
    print(f"    payer email : {payer.get('email_address')}")
    print(f"    payer id    : {payer.get('payer_id')}")
    if capture["status"] != "COMPLETED":
        raise SystemExit(f"Capture not completed: {capture['status']}.")

    print("\nNext: file a dispute against this capture with")
    print(f"  uv run python scripts/demo_dispute.py --capture-id {capture_id} "
          f"--amount <amount> --buyer-email {payer.get('email_address')}")


if __name__ == "__main__":
    main()
