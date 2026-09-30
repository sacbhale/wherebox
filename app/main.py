"""App factory. Run with: uvicorn app.main:create_app --factory"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.ai.registry import get_provider
from app.api.routes import router as api_router
from app.auth.middleware import auth_middleware
from app.auth.routes import init_auth
from app.auth.routes import router as auth_router
from app.auth.throttle import LoginThrottle
from app.capture.routes import router as capture_router
from app.capture.worker import Worker, reset_processing
from app.config import Settings
from app.db.connection import connect
from app.db.migrate import migrate
from app.db.timestamps import utcnow
from app.inventory.review_routes import router as review_router
from app.inventory.routes import router as inventory_router
from app.settings_routes import router as settings_router

STATIC_DIR = Path(__file__).parent / "static"

health = APIRouter()


@health.get("/healthz")
def healthz(request: Request) -> dict[str, str]:
    with closing(connect(request.app.state.settings.db_path)) as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    for directory in (settings.data_dir, settings.photos_dir, settings.backups_dir):
        directory.mkdir(parents=True, exist_ok=True)
    with closing(connect(settings.db_path)) as conn:
        migrate(conn)
        init_auth(app, conn)
        reset_processing(conn)  # captures interrupted by the last shutdown are re-queued
    # The single capture worker thread; tests keep WORKER_AUTOSTART false and call run_once.
    if settings.worker_autostart:
        app.state.worker = Worker(settings, get_provider(settings), clock=lambda: app.state.clock())
        app.state.worker.start()
    try:
        yield
    finally:
        if app.state.worker is not None:
            app.state.worker.stop()
            app.state.worker = None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Feature routers are included here; middleware is added here too."""
    settings = settings or Settings()
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.clock = utcnow  # tests replace this to move session time
    app.state.throttle = LoginThrottle()
    app.state.password_set = False
    app.state.setup_token = None
    app.state.worker = None
    app.middleware("http")(auth_middleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health)
    app.include_router(auth_router)
    app.include_router(api_router)
    app.include_router(settings_router)
    app.include_router(inventory_router)
    app.include_router(capture_router)
    app.include_router(review_router)
    return app
