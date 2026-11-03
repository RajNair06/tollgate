# Toll Gate

A policy gateway between AI agents and PayPal. The gateway holds the PayPal
credentials and executes payments, so agents never touch money directly.

## What's done so far

- **PayPal REST client** (`gateway/paypal_client.py`)
  - OAuth2 client-credentials token acquisition, caching, and auto-refresh
    (60s before expiry)
  - Orders v2: create / get / capture
  - Payments v2: get capture, full and partial refunds
  - Disputes v1: get / list
  - Idempotency via `PayPal-Request-Id` — retries never move money twice
  - `Decimal` money handling, serialized as strings (never floats)
  - Typed error hierarchy (auth, rate limit, server, timeout) with
    `debug_id` logging
- **FastAPI skeleton** (`main.py`) with lifespan-managed client and a
  `/health` endpoint
- **End-to-end sandbox flow** (`scripts/toll_flow.py`): create order →
  approve in browser as sandbox buyer → capture → partial refund → replay
  the same request ID to prove idempotency → verify final state
- **9 offline unit tests** (`tests/test_paypal_client.py`) using
  `httpx.MockTransport` — token refresh and error handling, no network
- Settings loaded and validated from `.env` (`config.py`); sandbox and live
  modes select the base URL and never share credentials

## Requirements

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)
- A PayPal sandbox account (developer.paypal.com) with business + personal
  credentials

## Setup

```bash
uv sync
cp .env.example .env   # fill in your sandbox client ID/secret, PAYPAL_MODE=sandbox
```

## Usage

Run the FastAPI app:

```bash
uv run python main.py
```

Run the end-to-end sandbox payment flow (refuses to run outside sandbox):

```bash
uv run python scripts/toll_flow.py
```

Run the tests:

```bash
uv run pytest
```

## Roadmap

- Agent-facing policy layer (spend limits, approval rules)
- Webhooks for async payment/dispute events
- Persistence for orders, captures, and refunds
