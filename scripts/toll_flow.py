"""End-to-end sandbox toll payment flow.

Steps:
1. Create a PayPal order ($10.99, intent=CAPTURE).
2. Print the approval URL. YOU open it in a browser and log in with the
   sandbox BUYER (personal) account, then approve. The script polls the
   order until its status becomes APPROVED.
3. Capture the order — money moves. Store the capture ID.
4. Issue a partial refund ($3.00) with an explicit PayPal-Request-Id.
5. Replay the refund with the SAME PayPal-Request-Id. PayPal must return
   the stored result (same refund ID), not refund twice.
6. Fetch the capture and show its refunds list as final proof.

Run from the project root:  uv run python scripts/toll_flow.py
"""

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
    new_request_id,
)

TOLL_AMOUNT = Decimal("10.99")
REFUND_AMOUNT = Decimal("3.00")
CURRENCY = "USD"
APPROVAL_TIMEOUT_SECONDS = 300  # 5 minutes to approve in the browser
POLL_INTERVAL_SECONDS = 5


def wait_for_approval(client: PayPalClient, order_id: str, approval_url: str) -> dict:
    """Poll the order until the buyer approves it in the browser."""
    print("\n" + "=" * 70)
    print("ACTION REQUIRED — approve this order as the sandbox BUYER:")
    print(f"\n  {approval_url}\n")
    print("1. Open the URL in your browser.")
    print("2. Log in with your sandbox PERSONAL (buyer) account.")
    print("3. Approve the payment.")
    print(f"\nPolling order status every {POLL_INTERVAL_SECONDS}s "
          f"(timeout {APPROVAL_TIMEOUT_SECONDS}s)...")
    print("=" * 70)

    deadline = time.monotonic() + APPROVAL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        order = client.get_order(order_id)
        status = order.get("status")
        if status == "APPROVED":
            print(f"\nOrder {order_id} is APPROVED.")
            return order
        if status in ("VOIDED", "COMPLETED"):
            raise SystemExit(f"Unexpected order status: {status}")
        print(f"  status={status}, waiting...")
        time.sleep(POLL_INTERVAL_SECONDS)

    raise SystemExit("Timed out waiting for buyer approval. Re-run the script.")


def main() -> None:
    settings = get_settings()
    if settings.paypal_mode != "sandbox":
        raise SystemExit("Refusing to run: PAYPAL_MODE is not 'sandbox'.")

    client = PayPalClient(
        settings.paypal_client_id,
        settings.paypal_client_secret,
        settings.paypal_base_url,
    )

    # -- 1. Create order -----------------------------------------------------
    order = client.create_order(
        TOLL_AMOUNT,
        CURRENCY,
        description="Toll: Plaza 1, Lane 2",
        invoice_id=f"TOLL-{int(time.time())}",
    )
    order_id = order["id"]
    print(f"[1] Created order {order_id} | status={order['status']} "
          f"| amount={order['purchase_units'][0]['amount']['value']} "
          f"{order['purchase_units'][0]['amount']['currency_code']}")

    # -- 2. Buyer approval (manual, in browser) -------------------------------
    approval_url = get_approval_url(order)
    if not approval_url:
        raise SystemExit("No approval URL in order response.")
    wait_for_approval(client, order_id, approval_url)

    # -- 3. Capture -----------------------------------------------------------
    captured = client.capture_order(order_id)
    capture_id = get_capture_id(captured)
    capture_status = captured["purchase_units"][0]["payments"]["captures"][0]["status"]
    print(f"[3] Captured order {order_id} | order status={captured['status']} "
          f"| capture_id={capture_id} | capture status={capture_status}")
    if capture_status != "COMPLETED":
        raise SystemExit(f"Capture not completed: {capture_status}. Inspect and re-run.")

    # -- 4. Partial refund with an explicit PayPal-Request-Id -----------------
    refund_request_id = new_request_id()
    print(f"[4] Refunding {REFUND_AMOUNT} {CURRENCY} of capture {capture_id} "
          f"with PayPal-Request-Id={refund_request_id}")
    refund = client.refund_capture(
        capture_id,
        REFUND_AMOUNT,
        CURRENCY,
        note_to_payer="Partial toll refund",
        request_id=refund_request_id,
    )
    refund_id = refund["id"]
    print(f"    refund_id={refund_id} | status={refund['status']} "
          f"| amount={refund['amount']['value']} {refund['amount']['currency_code']}")

    # -- 5. Replay the SAME refund request (idempotency proof) -----------------
    print(f"[5] Replaying refund with the SAME PayPal-Request-Id={refund_request_id}")
    replay = client.refund_capture(
        capture_id,
        REFUND_AMOUNT,
        CURRENCY,
        note_to_payer="Partial toll refund",
        request_id=refund_request_id,
    )
    replay_id = replay["id"]
    print(f"    replay refund_id={replay_id} | status={replay['status']}")
    if replay_id == refund_id:
        print("    IDEMPOTENT: same refund ID returned. No second refund happened.")
    else:
        print("    WARNING: different refund ID — money may have moved twice!")

    # -- 6. Final state: capture status + the refund itself --------------------
    # The capture resource does not embed refunds; verify via its status and
    # by fetching the refund by the ID stored in step 4.
    final = client.get_capture(capture_id)
    refund_final = client.get_refund(refund_id)
    print(f"[6] Capture {capture_id}: status={final['status']} "
          f"(PARTIALLY_REFUNDED = the partial refund landed, capture not exhausted)")
    print(f"    Refund {refund_final['id']}: status={refund_final['status']} "
          f"| amount={refund_final['amount']['value']} {refund_final['amount']['currency_code']}")

    client.close()
    print("\nFlow complete.")


if __name__ == "__main__":
    main()
