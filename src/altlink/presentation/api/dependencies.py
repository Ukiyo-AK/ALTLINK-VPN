from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from altlink.application.services.base import AuthError
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


external_api_key_header = APIKeyHeader(
    name="X-API-Key",
    scheme_name="ExternalApiKey",
    description="Персональный API-ключ, созданный в админ-панели ALTLINK.",
    auto_error=False,
)


@dataclass(frozen=True, slots=True)
class ExternalApiPrincipal:
    client_id: str
    name: str
    scopes: frozenset[str]
    expires_at: datetime | None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


async def require_external_api_client(
    request: Request,
    api_key: str | None = Depends(external_api_key_header),
    container: AppContainer = Depends(get_container),
) -> ExternalApiPrincipal:
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Передайте API-ключ в заголовке X-API-Key.",
        )
    source_ip = request.client.host if request.client else None
    async with container.hub() as hub:
        try:
            client = await hub.external_api.authenticate(api_key, source_ip=source_ip)
        except AuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
            ) from exc
        return ExternalApiPrincipal(
            client_id=client.id,
            name=client.name,
            scopes=frozenset(client.scopes or []),
            expires_at=client.expires_at,
        )
