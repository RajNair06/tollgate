"""Application settings, loaded from environment variables (.env)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# The mode selects the base URL. Sandbox and live never share credentials.
BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}


@dataclass(frozen=True)
class Settings:
    paypal_client_id: str
    paypal_client_secret: str
    paypal_mode: str
    paypal_base_url: str
    database_url: str
    redis_url: str
    rate_limit_per_agent_per_min: int
    rate_limit_global_per_min: int
    # Bootstrap credential for the /v1/admin routes. Deliberately an env var,
    # NOT an agent key: root trust must come from deployment config (you can't
    # create the admin credential through the admin API — chicken and egg).
    # Empty string = admin routes disabled (fail-safe default).
    admin_api_key: str
    # Webhook signature verification, dual strategy (fail-closed if neither
    # is configured — the endpoint rejects rather than accepting unverified):
    # - paypal_webhook_id: production path. PayPal signs each delivery with
    #   its private key; we POST the transmission headers + raw body + this
    #   id to PayPal's verify-webhook-signature API and trust only its OK.
    # - webhook_shared_secret: dev/simulation path. scripts/simulate_dispute.py
    #   signs the raw body with HMAC-SHA256 under this secret. PayPal cannot
    #   sign locally simulated events (and sandbox cannot reach a local URL),
    #   so the simulator needs a verifier that exercises the SAME endpoint,
    #   parsing, and persistence path — only the crypto check differs.
    paypal_webhook_id: str
    webhook_shared_secret: str
    # The agent's telemetry database (agent_runs). The dashboard endpoints
    # READ it to show run reasoning/confidence next to pending approvals —
    # a cross-database join done in Python, because the gateway schema must
    # not grow foreign keys into a database another process owns.
    agent_database_url: str


def get_settings() -> Settings:
    """Read settings from the environment and validate them."""
    client_id = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("PAYPAL_CLIENT_SECRET", "").strip()
    mode = os.environ.get("PAYPAL_MODE", "sandbox").strip().lower()

    if not client_id or not client_secret:
        raise ValueError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set")
    if mode not in BASE_URLS:
        raise ValueError(f"PAYPAL_MODE must be one of {list(BASE_URLS)}, got {mode!r}")

    return Settings(
        paypal_client_id=client_id,
        paypal_client_secret=client_secret,
        paypal_mode=mode,
        paypal_base_url=BASE_URLS[mode],
        database_url=os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://tollgate:tollgate@localhost:5433/tollgate",
        ).strip(),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6380/0").strip(),
        rate_limit_per_agent_per_min=int(
            os.environ.get("RATE_LIMIT_PER_AGENT_PER_MIN", "60")
        ),
        rate_limit_global_per_min=int(
            os.environ.get("RATE_LIMIT_GLOBAL_PER_MIN", "600")
        ),
        admin_api_key=os.environ.get("ADMIN_API_KEY", "").strip(),
        paypal_webhook_id=os.environ.get("PAYPAL_WEBHOOK_ID", "").strip(),
        webhook_shared_secret=os.environ.get("WEBHOOK_SHARED_SECRET", "").strip(),
        agent_database_url=os.environ.get(
            "AGENT_DATABASE_URL",
            "postgresql://tollgate:tollgate@localhost:5433/agent",
        ).strip(),
    )
