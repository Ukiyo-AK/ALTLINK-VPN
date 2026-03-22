from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware import Middleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.sessions import SessionMiddleware

from altlink.application.services import BootstrapService
from altlink.core.logging import configure_logging
from altlink.infrastructure.db import models  # noqa: F401
from altlink.infrastructure.db.session import create_engine_and_factory
from altlink.infrastructure.remnawave import RemnawaveClient
from altlink.presentation.api.routers.admin import router as admin_api_router
from altlink.presentation.api.routers.health import router as health_router
from altlink.presentation.web.router import router as web_router
from altlink.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    engine, session_factory = create_engine_and_factory(settings)
    remnawave = RemnawaveClient(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.remnawave = remnawave
    async with session_factory() as session:
        bootstrap = BootstrapService(session, settings, remnawave)
        await bootstrap.ensure_defaults()
        await session.commit()
    yield
    await remnawave.close()
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    middleware = [
        Middleware(GZipMiddleware, minimum_size=1024),
        Middleware(
            SessionMiddleware,
            secret_key=settings.secret_key,
            session_cookie=settings.session_cookie_name,
            max_age=settings.session_max_age_seconds,
            same_site="lax",
            https_only=settings.app_env == "production",
        ),
    ]
    app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan, middleware=middleware)
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    app.include_router(health_router)
    app.include_router(admin_api_router)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory="src/altlink/presentation/web/static"), name="static")
    return app
