"""Tests for PayPalClient using httpx.MockTransport — no network.

Two suites:
1. Token refresh: caching, expiry, 401 retry-once, token endpoint failure.
2. Error handling: typed mapping, debug_id/details propagation, timeouts.
"""

import httpx
import pytest

from gateway.paypal_client import (
    PayPalAPIError,
    PayPalAuthError,
    PayPalClient,
    PayPalRateLimitError,
    PayPalServerError,
    PayPalTimeoutError,
)
from decimal import Decimal

BASE_URL = "https://api-m.test.paypal.com"
TOKEN_PATH = "/v1/oauth2/token"


def token_response(token: str, expires_in: int = 32400) -> httpx.Response:
    return httpx.Response(
        200, json={"access_token": token, "token_type": "Bearer", "expires_in": expires_in}
    )


def make_client(handler) -> PayPalClient:
    """Build a PayPalClient whose HTTP layer is the mock handler."""
    return PayPalClient("id", "secret", BASE_URL, transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# 1. Token refresh
# ---------------------------------------------------------------------------

def test_token_fetched_once_and_cached_across_calls():
    """Two API calls share one token request; auth headers are correct."""
    counts = {"token": 0, "api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            counts["token"] += 1
            assert request.headers["authorization"].startswith("Basic ")
            assert request.content == b"grant_type=client_credentials"
            return token_response("tok-1")
        counts["api"] += 1
        assert request.headers["authorization"] == "Bearer tok-1"
        return httpx.Response(200, json={"id": "ORDER-1", "status": "CREATED"})

    client = make_client(handler)
    client.get_order("ORDER-1")
    client.get_order("ORDER-1")

    assert counts == {"token": 1, "api": 2}


def test_expired_token_is_refetched_before_next_call():
    """expires_in=0 lands inside the 60s safety margin, so every call refetches."""
    tokens = iter(["tok-1", "tok-2"])
    seen_bearer = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response(next(tokens), expires_in=0)
        seen_bearer.append(request.headers["authorization"])
        return httpx.Response(200, json={})

    client = make_client(handler)
    client.get_order("A")
    client.get_order("B")

    assert seen_bearer == ["Bearer tok-1", "Bearer tok-2"]


def test_401_refreshes_token_and_retries_once():
    """A stale token (401) triggers one forced refresh and one retry."""
    counts = {"token": 0, "api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            counts["token"] += 1
            return token_response(f"tok-{counts['token']}")
        counts["api"] += 1
        if request.headers["authorization"] == "Bearer tok-1":
            return httpx.Response(
                401, json={"name": "INVALID_TOKEN", "message": "expired", "debug_id": "d-1"}
            )
        return httpx.Response(200, json={"id": "ORDER-1"})

    client = make_client(handler)
    result = client.get_order("ORDER-1")

    assert result == {"id": "ORDER-1"}
    assert counts == {"token": 2, "api": 2}  # exactly one retry, no loop


def test_persistent_401_raises_auth_error_after_one_retry():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response("tok-x")
        return httpx.Response(
            401, json={"name": "INVALID_TOKEN", "message": "nope", "debug_id": "d-2"}
        )

    client = make_client(handler)
    with pytest.raises(PayPalAuthError) as exc_info:
        client.get_order("ORDER-1")

    assert exc_info.value.status_code == 401
    assert exc_info.value.debug_id == "d-2"


def test_token_endpoint_failure_raises_auth_error():
    """Bad credentials: OAuth-style body (error/error_description, no debug_id)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": "invalid_client", "error_description": "Client Authentication failed"}
        )

    client = make_client(handler)
    with pytest.raises(PayPalAuthError) as exc_info:
        client.get_access_token()

    assert exc_info.value.name == "invalid_client"
    assert exc_info.value.debug_id is None


# ---------------------------------------------------------------------------
# 2. Error handling
# ---------------------------------------------------------------------------

def test_error_body_fields_are_mapped():
    """name, message, debug_id, and details survive into the exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response("tok-1")
        return httpx.Response(
            422,
            json={
                "name": "UNPROCESSABLE_ENTITY",
                "message": "business validation failed",
                "debug_id": "abc123",
                "details": [{"issue": "CAPTURE_ALREADY_REFUNDED", "description": "no more funds"}],
            },
        )

    client = make_client(handler)
    with pytest.raises(PayPalAPIError) as exc_info:
        client.refund_capture("CAP-1", Decimal("3.00"))

    err = exc_info.value
    assert err.status_code == 422
    assert err.name == "UNPROCESSABLE_ENTITY"
    assert err.debug_id == "abc123"
    assert err.details[0]["issue"] == "CAPTURE_ALREADY_REFUNDED"


@pytest.mark.parametrize(
    ("status", "expected_class"),
    [
        (404, PayPalAPIError),        # client mistake, base class
        (429, PayPalRateLimitError),  # back off
        (500, PayPalServerError),     # PayPal-side; safe to retry with same request id
        (503, PayPalServerError),
    ],
)
def test_status_codes_map_to_typed_errors(status, expected_class):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response("tok-1")
        return httpx.Response(status, json={"name": "SOME_ERROR", "message": "m", "debug_id": "d"})

    client = make_client(handler)
    with pytest.raises(expected_class) as exc_info:
        client.get_order("ORDER-1")
    assert exc_info.value.status_code == status
    # 404 must not be mistaken for an auth error (which would trigger a retry)
    if status == 404:
        assert not isinstance(exc_info.value, PayPalAuthError)


def test_timeout_raises_with_request_id_guidance():
    """On timeout the outcome is unknown; the message must name the request id
    so the caller can retry idempotently."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response("tok-1")
        raise httpx.ConnectTimeout("connection timed out", request=request)

    client = make_client(handler)
    with pytest.raises(PayPalTimeoutError) as exc_info:
        client.create_order(Decimal("10.99"), request_id="fixed-uuid-123")

    assert "fixed-uuid-123" in str(exc_info.value)


def test_posts_always_carry_a_paypal_request_id():
    """Idempotency header present even when the caller supplies none."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response("tok-1")
        seen.append(request.headers.get("paypal-request-id"))
        return httpx.Response(201, json={"id": "ORDER-1", "status": "CREATED"})

    client = make_client(handler)
    client.create_order(Decimal("10.99"))

    assert seen[0] is not None and len(seen[0]) == 36  # UUID
