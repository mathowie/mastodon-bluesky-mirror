"""Web dashboard and one polling worker per container."""
import asyncio
import logging
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import init_db
from .core.syncer import SyncOrchestrator
from .core.image_runtime import image_status
from .web.routes import router as web_router

logging.basicConfig(level=logging.INFO)
# SDK request logs add noise to the useful polling summaries.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("masto-bsky-mirror")


async def sync_loop(wakeup=None):
    wakeup = wakeup or asyncio.Event()
    # The worker reuses both API clients so each poll does not create another
    # Bluesky session. Each cycle gets its own SQLite session in its thread.
    orchestrator = None

    def run_once():
        nonlocal orchestrator
        from .database import SessionLocal
        with SessionLocal() as db:
            if orchestrator is None:
                orchestrator = SyncOrchestrator(db)
            else:
                orchestrator.db = db
            orchestrator.run_sync_cycle()

    while True:
        wakeup.clear()
        logger.info("Background sync check starting")
        try:
            # Both SDKs perform blocking I/O. Keep the web event loop available.
            await asyncio.to_thread(run_once)
        except Exception as exc:
            logger.error("Background sync check failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(wakeup.wait(), timeout=max(0.01, settings.SYNC_INTERVAL_SECONDS))
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app):
    init_db()
    images = image_status()
    if not images["ready"]:
        logger.error("%s", images["error"])
    else:
        logger.info("Image processing ready (Pillow %s)", images["pillow_version"])
    logger.info("Sync worker enabled; only posts since first activation are eligible")
    app.state.sync_wakeup = asyncio.Event()
    task = asyncio.create_task(sync_loop(app.state.sync_wakeup))
    app.state.sync_task = task
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Mastodon to Bluesky Mirror", lifespan=lifespan)
security = HTTPBasic()


def authenticate(credentials: HTTPBasicCredentials = Security(security)):
    username_ok = secrets.compare_digest(credentials.username.encode(), b"admin")
    password_ok = secrets.compare_digest(
        credentials.password.encode(), settings.DASHBOARD_PASSWORD.encode(),
    )
    if username_ok and password_ok:
        return credentials
    raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


app.include_router(web_router, prefix="/dashboard", dependencies=[Depends(authenticate)])
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")


@app.get("/health")
async def health_check():
    images = image_status()
    return JSONResponse(
        {"status": "ok" if images["ready"] else "degraded", "image_processing": images},
        status_code=200 if images["ready"] else 503,
    )
