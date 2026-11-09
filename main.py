"""FastAPI entry point for the toll gate payment gateway."""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request

from config import get_settings
from gateway.admin import admin_router
from gateway.db import get_engine
from gateway.mcp_server import init_paypal_client
from gateway.mcp_server import mcp as mcp_server
from gateway.paypal_client import PayPalClient
from gateway.redis_client import RedisTokenCache
from gateway.routes import router
from gateway.dashboard import dashboard_router
from gateway.webhooks import webhook_router

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
    # MCP tools call PayPal through this injected client (shared token cache).
    init_paypal_client(client)
    try:
        # The mounted MCP sub-app's lifespan does NOT run automatically under
        # FastAPI — enter it here so the streamable-HTTP session manager is
        # live before the first request.
        async with mcp_app.lifespan(app):
            yield
    finally:
        client.close()
        get_engine().dispose()


# MCP endpoint: streamable HTTP at /mcp. json_response=True: POST replies
# arrive as plain JSON (spec-legal) instead of SSE streams — friendlier for
# curl and every client we target.
mcp_app = mcp_server.http_app(path="/mcp", json_response=True)

app = FastAPI(title="Toll Gate Gateway", version="0.6.0", lifespan=lifespan)
app.include_router(router)
app.include_router(admin_router)
# Registered BEFORE the MCP catch-all mount below — Starlette matches routes
# in registration order, so /webhooks/* and /v1/admin/dashboard/* must not
# fall through to the sub-app.
app.include_router(webhook_router)
app.include_router(dashboard_router)


@app.get("/health")
def health(request: Request) -> dict:
    """Liveness probe. Reports the active PayPal mode, without calling PayPal."""
    settings = request.app.state.settings
    return {
        "status": "ok",
        "paypal_mode": settings.paypal_mode,
        "version": app.version,
    }


# Mounted LAST — Starlette matches routes in registration order, so /health
# and /v1/* keep priority and everything under /mcp falls through to the MCP
# sub-app. Must be the SAME instance the lifespan enters above.
app.mount("/", mcp_app)


def main():
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
