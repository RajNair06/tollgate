"""PayPal REST API client.

Covers:
- OAuth 2.0 client-credentials token acquisition
- Token caching with expiry handling (refresh 60s before expiry)
- Orders v2: create / get / capture
- Payments v2: get capture / refund capture (full and partial)
- Disputes v1: get / list
- HTTP timeout handling and PayPal error mapping with debug_id logging
- Decimal money, serialized as strings in request bodies
"""

from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Connect fast, allow slow reads for payment operations.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)

# Refresh the token this many seconds before it actually expires, so a token
# cannot die mid-flight during a request.
TOKEN_EXPIRY_MARGIN_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------

def to_paypal_amount(value: Decimal) -> str:
    """Serialize a Decimal as a PayPal amount string.

    PayPal requires string values like "10.99" — never floats, which carry
    binary rounding errors. Quantize to 2 decimal places with banker-safe
    ROUND_HALF_UP, then render as a plain string.
    """
    if not isinstance(value, Decimal):
        raise TypeError(f"monetary values must be Decimal, got {type(value).__name__}")
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(quantized)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PayPalError(Exception):
    """Base class for all PayPal client errors."""


class PayPalTimeoutError(PayPalError):
    """The request timed out. The outcome on PayPal's side is UNKNOWN.

    Safe recovery: retry with the SAME PayPal-Request-Id so PayPal returns
    the stored result instead of executing twice.
    """


class PayPalAPIError(PayPalError):
    """PayPal returned an HTTP 4xx/5xx with an error body."""

    def __init__(
        self,
        status_code: int,
        name: str,
        message: str,
        debug_id: str | None,
        details: list[dict] | None = None,
    ):
        self.status_code = status_code
        self.name = name
        self.message = message
        self.debug_id = debug_id
        self.details = details or []
        super().__init__(f"[{status_code}] {name}: {message} (debug_id={debug_id})")


class PayPalAuthError(PayPalAPIError):
    """401/403: token or credentials problem."""


class PayPalRateLimitError(PayPalAPIError):
    """429: back off and retry later."""


class PayPalServerError(PayPalAPIError):
    """5xx: PayPal-side failure. Retry with the same PayPal-Request-Id."""


# Map error name prefixes / status codes to exception classes.
_STATUS_TO_ERROR = {
    401: PayPalAuthError,
    403: PayPalAuthError,
    429: PayPalRateLimitError,
    500: PayPalServerError,
    503: PayPalServerError,
}


def new_request_id() -> str:
    """Generate a UUID for the PayPal-Request-Id header (idempotency)."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class PayPalClient:
    """Synchronous PayPal REST client backed by one httpx.Client."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"Accept": "application/json"},
            transport=transport,  # test seam: httpx.MockTransport in tests
        )
        # Token cache. Uses the monotonic clock: immune to wall-clock changes.
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def close(self) -> None:
        self._http.close()

    # -- OAuth: token acquisition and caching -------------------------------

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Return a valid access token, fetching a new one only when needed.

        Caching rules:
        - Reuse the cached token while now < expires_at (expiry already
          includes a 60-second safety margin).
        - Otherwise POST /v1/oauth2/token with client credentials (Basic auth,
          grant_type=client_credentials) and cache the fresh token.
        """
        now = time.monotonic()
        if not force_refresh and self._access_token and now < self._token_expires_at:
            logger.debug("Reusing cached access token (%.0fs left)", self._token_expires_at - now)
            return self._access_token

        logger.info("Requesting new PayPal access token")
        try:
            response = self._http.post(
                "/v1/oauth2/token",
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException as exc:
            raise PayPalTimeoutError("Timed out requesting access token") from exc

        if response.status_code >= 400:
            # The token endpoint returns OAuth-style errors:
            # {"error": "...", "error_description": "..."} without a debug_id.
            payload = _safe_json(response)
            name = payload.get("error", "INVALID_RESPONSE")
            message = payload.get("error_description", response.text[:200])
            logger.error("Token request failed: [%s] %s: %s", response.status_code, name, message)
            raise PayPalAuthError(response.status_code, name, message, debug_id=None)

        data = response.json()
        self._access_token = data["access_token"]
        self._token_expires_at = now + float(data["expires_in"]) - TOKEN_EXPIRY_MARGIN_SECONDS
        logger.info("Access token acquired, valid for %ss", data["expires_in"])
        return self._access_token

    # -- Core request plumbing ----------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict | None = None,
        request_id: str | None = None,
        prefer_representation: bool = False,
    ) -> dict:
        """Send an authenticated request with 401-refresh, timeout handling,
        and error mapping. POSTs always carry a PayPal-Request-Id.

        prefer_representation=True asks PayPal (via the Prefer header) to
        return the full resource instead of a minimal {id, status} body.
        """
        headers: dict[str, str] = {}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        if prefer_representation:
            headers["Prefer"] = "return=representation"

        last_error: PayPalAPIError | None = None
        for attempt in (1, 2):
            token = self.get_access_token(force_refresh=(attempt == 2))
            headers["Authorization"] = f"Bearer {token}"
            try:
                response = self._http.request(
                    method, path, json=json_body, params=params, headers=headers
                )
            except httpx.TimeoutException as exc:
                raise PayPalTimeoutError(
                    f"{method} {path} timed out. Outcome unknown — retry with the "
                    f"same PayPal-Request-Id ({request_id!r})."
                ) from exc

            if response.status_code < 400:
                if response.status_code == 204 or not response.content:
                    return {}
                return response.json()

            error = _map_error(method, path, response)
            # One retry with a fresh token on auth failures only.
            if attempt == 1 and isinstance(error, PayPalAuthError) and error.status_code == 401:
                logger.warning("Got 401 (%s) — refreshing token and retrying once", error.name)
                last_error = error
                continue
            raise error

        assert last_error is not None
        raise last_error

    # -- Orders v2 ------------------------------------------------------------

    def create_order(
        self,
        amount: Decimal,
        currency_code: str = "USD",
        *,
        description: str | None = None,
        invoice_id: str | None = None,
        custom_id: str | None = None,
        payment_source: dict | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Create an order with intent=CAPTURE. No money moves yet.

        Returns the order resource: id, status=CREATED, links (including the
        'payer-action' approval URL).

        payment_source: optional card/wallet source, e.g.
        {"card": {"number": ..., "expiry": "2030-01", ...}}. A card order
        can capture immediately without buyer redirect (Expanded Checkout;
        sandbox test cards capture directly).
        """
        purchase_unit: dict[str, Any] = {
            "amount": {
                "currency_code": currency_code,
                "value": to_paypal_amount(amount),  # Decimal -> "10.99"
            }
        }
        if description:
            purchase_unit["description"] = description
        if invoice_id:
            purchase_unit["invoice_id"] = invoice_id
        if custom_id:
            purchase_unit["custom_id"] = custom_id

        body: dict[str, Any] = {"intent": "CAPTURE", "purchase_units": [purchase_unit]}
        if payment_source:
            body["payment_source"] = payment_source
        return self._request(
            "POST",
            "/v2/checkout/orders",
            json_body=body,
            request_id=request_id or new_request_id(),
            prefer_representation=True,
        )

    def get_order(self, order_id: str) -> dict:
        """Fetch current order details. Valid at any stage of the order."""
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def capture_order(self, order_id: str, *, request_id: str | None = None) -> dict:
        """Capture an APPROVED order — this moves the money.

        The response contains purchase_units[0].payments.captures[0].id:
        the capture ID you must store for refunds.
        """
        return self._request(
            "POST",
            f"/v2/checkout/orders/{order_id}/capture",
            json_body={},
            request_id=request_id or new_request_id(),
            prefer_representation=True,
        )

    # -- Payments v2: captures and refunds -------------------------------------

    def get_capture(self, capture_id: str) -> dict:
        """Fetch capture details.

        Note: the capture resource does NOT embed its refunds. Its 'status'
        field (e.g. PARTIALLY_REFUNDED) is the refund signal; fetch each
        refund with get_refund() by the IDs you stored.
        """
        return self._request("GET", f"/v2/payments/captures/{capture_id}")

    def get_refund(self, refund_id: str) -> dict:
        """Fetch a refund by its own ID (GET /v2/payments/refunds/{id})."""
        return self._request("GET", f"/v2/payments/refunds/{refund_id}")

    def refund_capture(
        self,
        capture_id: str,
        amount: Decimal | None = None,
        currency_code: str = "USD",
        *,
        note_to_payer: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Refund a capture, by capture ID (not order ID).

        amount=None  -> full refund (empty body).
        amount=Decimal -> partial refund. Repeats allowed until the capture
        is exhausted.

        Pass an explicit request_id to replay a refund safely: PayPal returns
        the stored result instead of refunding twice.
        """
        body: dict[str, Any] = {}
        if amount is not None:
            body["amount"] = {
                "value": to_paypal_amount(amount),
                "currency_code": currency_code,
            }
        if note_to_payer:
            body["note_to_payer"] = note_to_payer

        return self._request(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            json_body=body,
            request_id=request_id or new_request_id(),
            prefer_representation=True,
        )

    # -- Disputes v1 -----------------------------------------------------------

    def get_dispute(self, dispute_id: str) -> dict:
        """Fetch one dispute by ID."""
        return self._request("GET", f"/v1/customer/disputes/{dispute_id}")

    def list_disputes(
        self,
        *,
        page_size: int = 20,
        start_time: str | None = None,
    ) -> dict:
        """List disputes for the business account.

        start_time: RFC 3339 timestamp, e.g. "2026-10-01T00:00:00Z".
        The response contains 'items' plus HATEOAS links for paging.
        """
        params: dict[str, Any] = {"page_size": page_size}
        if start_time:
            params["start_time"] = start_time
        return self._request("GET", "/v1/customer/disputes", params=params)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _safe_json(response: httpx.Response) -> dict:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except ValueError:
        return {}


def _map_error(method: str, path: str, response: httpx.Response) -> PayPalAPIError:
    """Convert an error response into a typed exception and log the debug_id.

    PayPal error body: {name, message, debug_id, details[], links[]}.
    The debug_id is meaningless to us but lets PayPal support find this exact
    request in their logs — always log it.
    """
    payload = _safe_json(response)
    name = payload.get("name", "UNKNOWN_ERROR")
    message = payload.get("message", response.text[:200])
    debug_id = payload.get("debug_id")
    details = payload.get("details", [])

    logger.error(
        "PayPal API error: %s %s -> %s name=%s debug_id=%s details=%s",
        method, path, response.status_code, name, debug_id, details,
    )

    error_class = _STATUS_TO_ERROR.get(response.status_code, PayPalAPIError)
    return error_class(
        status_code=response.status_code,
        name=name,
        message=message,
        debug_id=debug_id,
        details=details,
    )


def get_approval_url(order: dict) -> str | None:
    """Extract the buyer approval URL from an order.

    Pure REST flows use rel='approve'; JS SDK flows use rel='payer-action'.
    Accept either.
    """
    links = order.get("links", [])
    for rel in ("payer-action", "approve"):
        for link in links:
            if link.get("rel") == rel:
                return link["href"]
    return None


def get_capture_id(order: dict) -> str | None:
    """Extract the first capture ID from a captured order response."""
    for unit in order.get("purchase_units", []):
        for capture in unit.get("payments", {}).get("captures", []):
            return capture.get("id")
    return None
