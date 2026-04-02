"""
Relay service — webhook ingestion + broker submission only.

Latency-critical. No background tasks, no frontend, no dashboard routes.
Reads stream prices from Redis (written by worker).
Publishes SSE events to Redis (consumed by dashboard).
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.models.db import init_db
from app.routers import webhook

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Relay service started")
    yield
    from app.redis import close_redis
    await close_redis()
    logger.info("Relay service stopped")


app = FastAPI(
    title="TV Broker Relay — Relay",
    version="0.4.0",
    lifespan=lifespan,
)

app.include_router(webhook.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "relay"}
