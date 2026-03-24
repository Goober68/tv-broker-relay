import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.models.db import init_db
from app.routers import webhook, status, auth, api_keys, broker_accounts, admin, billing

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    from app.services.background_tasks import start_background_tasks
    tasks = start_background_tasks()
    logger.info(f"Started {len(tasks)} background tasks")
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Background tasks stopped")


app = FastAPI(
    title="TradingView Broker Relay",
    description="Multi-tenant TradingView webhook relay with broker execution.",
    version="0.3.0",
    lifespan=lifespan,
)

# ── API routes (must be registered before the SPA catch-all) ──────────────────
app.include_router(auth.router)
app.include_router(api_keys.router)
app.include_router(broker_accounts.router)
app.include_router(billing.router)
app.include_router(admin.router)
app.include_router(webhook.router)
app.include_router(status.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Serve built frontend ───────────────────────────────────────────────────────
# Assets (JS, CSS, images) are served from /assets/* with long cache headers.
# All other non-API routes return index.html for client-side routing (SPA fallback).

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        # Serve index.html for all non-API, non-asset routes
        index = STATIC_DIR / "index.html"
        if index.exists():
            return FileResponse(index)
        return {"detail": "Frontend not built. Run: cd frontend && npm run build"}
else:
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_not_built(full_path: str):
        return {"detail": "Frontend not built. Run: cd frontend && npm run build"}
