"""FastAPI entry point for the toll gate payment gateway."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

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
from worker.inprocess import start_in_process_worker, worker_requested

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
    # Opt-in embedded outbox worker (WORKER_IN_PROCESS=1) — exists solely
    # so hosts without background workers (Render's free plan) can still
    # run the webhook pipeline. Missing credentials log loudly and skip
    # the thread rather than crashing the web service: the agent key is
    # provisioned through THIS service's admin API, so a hard failure
    # would deadlock the first deploy. The separate `python -m worker`
    # process remains the recommended shape (see README → Deployment).
    worker_stop = None
    if worker_requested():
        started = start_in_process_worker()
        if started is not None:
            _, worker_stop = started
    try:
        # The mounted MCP sub-app's lifespan does NOT run automatically under
        # FastAPI — enter it here so the streamable-HTTP session manager is
        # live before the first request.
        async with mcp_app.lifespan(app):
            yield
    finally:
        if worker_stop is not None:
            worker_stop.set()  # ends the poll loop; the daemon thread exits
        client.close()
        get_engine().dispose()


# MCP endpoint: streamable HTTP at /mcp. json_response=True: POST replies
# arrive as plain JSON (spec-legal) instead of SSE streams — friendlier for
# curl and every client we target.
mcp_app = mcp_server.http_app(path="/mcp", json_response=True)

app = FastAPI(title="Toll Gate Gateway", version="0.7.0", lifespan=lifespan)
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


# Root home card. Registered BEFORE the MCP catch-all mount — the same
# "route order beats mount order" rule the /dashboard redirect relies on.
# The project's full documentation lives on GitHub Pages, which does not
# depend on this process being awake; this page just says "the API is
# alive" and points at everything.
DOCS_URL = "https://rajnair06.github.io/tollgate/"
REPO_URL = "https://github.com/RajNair06/tollgate"

ROOT_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Toll Gate — API</title>
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="icon" type="image/png" sizes="64x64" href="/dashboard/favicon.png">
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; background: #0e1117; color: #e6e8ee;
         font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  .card { max-width: 660px; margin: 24px; padding: 32px 36px;
          background: #151a23; border: 1px solid #2a3140; border-radius: 14px; }
  h1 { margin: 0 0 4px; font-size: 28px; }
  .sub { color: #9aa1b1; margin: 0 0 24px; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
         background: #3ddc84; margin-right: 8px; }
  ul { list-style: none; padding: 0; margin: 0; }
  li { margin: 10px 0; }
  a { color: #7ab8ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .tag { color: #9aa1b1; font-size: 13px; margin-left: 8px; }
</style>
</head>
<body>
<div class="card">
  <h1><img src="/dashboard/logo.png" alt="" style="height:36px;width:36px;vertical-align:-9px;margin-right:10px"><span class="dot"></span>Toll Gate API</h1>
  <p class="sub">A policy gateway between AI agents and PayPal —
     version __VERSION__, live.</p>
  <ul>
    <li><a href="__DOCS_URL__">Documentation</a>
        <span class="tag">what it is, how it works, get started — static,
        always up (GitHub Pages)</span></li>
    <li><a href="/dashboard/">Admin dashboard</a>
        <span class="tag">approvals, actions, audit, dead letter (admin key
        required)</span></li>
    <li><a href="/docs">OpenAPI reference</a>
        <span class="tag">interactive API docs served by this app</span></li>
    <li><a href="/health">Health</a>
        <span class="tag">liveness probe (JSON)</span></li>
    <li><a href="__REPO_URL__">Source on GitHub</a>
        <span class="tag">MIT licensed</span></li>
  </ul>
</div>
</body>
</html>
"""


@app.get("/", include_in_schema=False)
def root() -> HTMLResponse:
    return HTMLResponse(
        ROOT_PAGE
        .replace("__VERSION__", app.version)
        .replace("__DOCS_URL__", DOCS_URL)
        .replace("__REPO_URL__", REPO_URL)
    )


# Static admin dashboard (increment 9). Mounted BEFORE the MCP catch-all —
# Starlette matches in registration order. html=True serves index.html at
# /dashboard/; all assets (including vendored AG Grid) are local files, so
# the page never reaches the network for anything but the JSON APIs.
DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    # Browsers ask for /favicon.ico unconditionally. The icon lives with the
    # dashboard assets so the repo keeps exactly one copy.
    return FileResponse(DASHBOARD_DIR / "favicon.ico")


@app.get("/dashboard", include_in_schema=False)
def dashboard_redirect():
    # Without this, bare /dashboard fully matches the catch-all Mount("/")
    # first and 404s inside the MCP sub-app — the Mount's own slash
    # redirect never gets a turn. Route order beats mount order.
    return RedirectResponse("/dashboard/")


app.mount("/dashboard", StaticFiles(directory=str(DASHBOARD_DIR), html=True),
          name="dashboard")

# Mounted LAST — Starlette matches routes in registration order, so /health
# and /v1/* keep priority and everything under /mcp falls through to the MCP
# sub-app. Must be the SAME instance the lifespan enters above.
app.mount("/", mcp_app)


def main():
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
