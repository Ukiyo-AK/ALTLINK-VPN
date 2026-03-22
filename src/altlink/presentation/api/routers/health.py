from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
async def healthcheck(request: Request) -> dict:
    return {
        "status": "ok",
        "app": request.app.state.settings.app_name,
        "env": request.app.state.settings.app_env,
    }


@router.get("/api/v1/health")
async def api_healthcheck(request: Request) -> dict:
    return {
        "status": "ok",
        "app": request.app.state.settings.app_name,
        "env": request.app.state.settings.app_env,
    }

