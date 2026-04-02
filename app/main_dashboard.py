"""
Dashboard service — user-facing API, SSE events, frontend.

No background tasks, no webhook processing.
Subscribes to Redis SSE channel for real-time browser updates.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.models.db import init_db
from app.routers import status, auth, api_keys, broker_accounts, admin, billing, oauth

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    tasks = []
    from app.services.events import start_sse_listener
    sse_task = start_sse_listener()
    if sse_task:
        tasks.append(sse_task)
    logger.info(f"Dashboard service started ({len(tasks)} background tasks)")
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    from app.redis import close_redis
    await close_redis()
    logger.info("Dashboard service stopped")


app = FastAPI(
    title="TV Broker Relay — Dashboard",
    version="0.4.0",
    lifespan=lifespan,
)

# ── API routes (registered before SPA catch-all) ─────────────────────────────
app.include_router(auth.router)
app.include_router(api_keys.router)
app.include_router(broker_accounts.router)
app.include_router(billing.router)
app.include_router(admin.router)
app.include_router(oauth.router)
app.include_router(status.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "dashboard"}


# ── Serve built frontend ─────────────────────────────────────────────────────
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")
    if (STATIC_DIR / "brokers").exists():
        app.mount("/brokers", StaticFiles(directory=STATIC_DIR / "brokers"), name="brokers")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon():
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        static_file = STATIC_DIR / full_path
        if static_file.is_file() and STATIC_DIR in static_file.resolve().parents:
            return FileResponse(static_file, headers={"Cache-Control": "public, max-age=86400"})
        index = STATIC_DIR / "index.html"
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return {"detail": "Frontend not built"}
