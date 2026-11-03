"""FastAPI entry point for the toll gate payment gateway."""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request

from config import get_settings
from gateway.paypal_client import PayPalClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build one PayPalClient at startup and close it at shutdown."""
    settings = get_settings()
    client = PayPalClient(
        client_id=settings.paypal_client_id,
        client_secret=settings.paypal_client_secret,
        base_url=settings.paypal_base_url,
    )
    app.state.settings = settings
    app.state.paypal = client
    try:
        yield
    finally:
        client.close()


app = FastAPI(title="Toll Gate Gateway", version="0.1.0", lifespan=lifespan)


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
