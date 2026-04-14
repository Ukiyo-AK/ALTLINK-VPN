from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status

from altlink.application.services.registry import AppContainer, ServiceHub


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


async def get_hub(container: AppContainer = Depends(get_container)) -> AsyncIterator[ServiceHub]:
    async with container.hub() as hub:
        yield hub


async def require_admin_api_key(
    x_admin_api_key: str | None = Header(default=None),
    container: AppContainer = Depends(get_container),
) -> None:
    if x_admin_api_key != container.settings.admin_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный API ключ.")

