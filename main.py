"""FastAPI entry point for the toll gate payment gateway."""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request

from config import get_settings
from gateway.admin import admin_router
from gateway.db import get_engine
from gateway.paypal_client import PayPalClient
from gateway.redis_client import RedisTokenCache
from gateway.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build shared resources once: PayPal client (Redis-backed token cache),
    DB engine. Close/dispose at shutdown."""
    settings = get_settings()
    client = PayPalClient(
        client_id=settings.paypal_client_id,
        client_secret=settings.paypal_client_secret,
        base_url=settings.paypal_base_url,
        # Token cached in Redis: shared across workers, TTL-enforced.
        # If Redis is down the cache fails open (fetches per call, logs).
        token_cache=RedisTokenCache(),
    )
    get_engine()  # fail fast if the database is unreachable at boot
    app.state.settings = settings
    app.state.paypal = client
    try:
        yield
    finally:
        client.close()
        get_engine().dispose()


app = FastAPI(title="Toll Gate Gateway", version="0.3.0", lifespan=lifespan)
app.include_router(router)
app.include_router(admin_router)


@app.get("/health")
def health(request: Request) -> dict:
    """Liveness probe. Reports the active PayPal mode, without calling PayPal."""
    settings = request.app.state.settings
    return {
        "status": "ok",
        "paypal_mode": settings.paypal_mode,
        "version": app.version,
    }


def main():
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
