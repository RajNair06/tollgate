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
    )
