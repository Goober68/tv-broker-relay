"""
Worker service — all background tasks + stream managers.

No HTTP routes (except /health for container healthcheck).
Runs fill polling, P&L engine, stream managers, reconciliation,
GTD expiry, token refresh, auto-close, daily summary, delivery purge.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.models.db import init_db

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    from app.services.background_tasks import start_background_tasks
    tasks = start_background_tasks()
    logger.info(f"Worker service started ({len(tasks)} background tasks)")
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    from app.redis import close_redis
    await close_redis()
    logger.info("Worker service stopped")


app = FastAPI(
    title="TV Broker Relay — Worker",
    version="0.4.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "worker"}
