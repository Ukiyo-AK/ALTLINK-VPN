from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from altlink.application.services.registry import AppContainer
from altlink.logging_config import configure_logging
from altlink.presentation.api.routes.admin_api import router as admin_api_router
from altlink.presentation.api.routes.external_api import router as external_api_router
from altlink.presentation.web.routes import router as admin_web_router
from altlink.settings import get_settings
from altlink.utils.media import media_root


class SimpleRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, limit: int = 10, window_seconds: int = 60) -> None:
        super().__init__(app)
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = {}

    async def dispatch(self, request: Request, call_next):
        if request.url.path != "/admin/login":
            return await call_next(request)

        now = request.scope["time"] if "time" in request.scope else None
        current = now.timestamp() if now else __import__("time").time()
        ip = request.client.host if request.client else "unknown"
        history = [stamp for stamp in self._hits.get(ip, []) if current - stamp <= self.window_seconds]
        if len(history) >= self.limit:
            return JSONResponse({"detail": "Слишком много попыток входа."}, status_code=429)
        history.append(current)
        self._hits[ip] = history
        return await call_next(request)


class CacheControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        elif response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.debug, settings=settings, service_name="backend")
    container = AppContainer(settings)
    app.state.settings = settings
    app.state.container = container
    yield
    await container.close()


def create_app() -> FastAPI:
    app = FastAPI(title="ALTLINK", lifespan=lifespan, docs_url="/docs", redoc_url=None)
    settings = get_settings()
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, same_site="lax")
    app.add_middleware(SimpleRateLimitMiddleware)
    app.add_middleware(CacheControlMiddleware)

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    shared_media_dir = media_root()
    if shared_media_dir.exists():
        app.mount("/media", StaticFiles(directory=shared_media_dir), name="media")

    app.include_router(admin_api_router)
    app.include_router(external_api_router)
    app.include_router(admin_web_router)

    @app.get("/health/live")
    async def health_live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready(request: Request):
        container: AppContainer = request.app.state.container
        async with container.session() as session:
            await session.execute(text("SELECT 1"))

        remnawave_ok = True
        if container.settings.remnawave_base_url and container.settings.remnawave_api_token:
            remnawave_ok = await container.remnawave.healthcheck()

        status_code = 200 if remnawave_ok else 503
        return JSONResponse({"database": True, "remnawave": remnawave_ok}, status_code=status_code)

    return app
