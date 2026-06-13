from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Protocol

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from altlink.infrastructure.remnawave_schemas import (
    RemoteAccessibleNode,
    RemoteConnectionKeys,
    RemoteHwidDevice,
    RemoteManagedInternalSquad,
    RemoteNode,
    RemoteNodeUsersIpsJob,
    RemoteNodeUserUsageRow,
    RemoteSubscriptionInfo,
    RemoteSubscriptionRequestRecord,
    RemoteUsageResponse,
    RemoteUser,
)
from altlink.settings import Settings

logger = logging.getLogger(__name__)


class RemnawaveGateway(Protocol):
    async def list_nodes(self) -> list[RemoteNode]: ...
    async def list_users(self) -> list[RemoteUser]: ...
    async def get_user(self, user_uuid: str) -> RemoteUser: ...
    async def find_user_by_telegram_id(self, telegram_id: int) -> RemoteUser | None: ...
    async def create_user(self, payload: dict) -> RemoteUser: ...
    async def update_user(self, payload: dict) -> RemoteUser: ...
    async def enable_user(self, user_uuid: str) -> RemoteUser: ...
    async def disable_user(self, user_uuid: str) -> RemoteUser: ...
    async def reset_user_traffic(self, user_uuid: str) -> RemoteUser: ...
    async def revoke_user_subscription(self, user_uuid: str) -> RemoteUser: ...
    async def delete_user(self, user_uuid: str) -> dict: ...
    async def get_accessible_nodes(self, user_uuid: str) -> list[RemoteAccessibleNode]: ...
    async def get_subscription_info(self, short_uuid: str) -> RemoteSubscriptionInfo: ...
    async def get_connection_keys(self, user_uuid: str) -> RemoteConnectionKeys: ...
    async def get_user_hwid_devices(self, user_uuid: str) -> list[RemoteHwidDevice]: ...
    async def delete_user_hwid_device(self, user_uuid: str, hwid: str) -> list[RemoteHwidDevice]: ...
    async def fetch_node_users_ips(self, node_uuid: str) -> str: ...
    async def get_node_users_ips_result(self, job_id: str) -> RemoteNodeUsersIpsJob: ...
    async def get_user_usage(self, user_uuid: str, start: date, end: date) -> RemoteUsageResponse: ...
    async def get_node_user_usage(self, node_uuid: str, start: datetime, end: datetime) -> list[RemoteNodeUserUsageRow] | None: ...
    async def get_subscription_request_history(self, user_uuid: str) -> list[RemoteSubscriptionRequestRecord]: ...
    async def list_internal_squads(self) -> list[RemoteManagedInternalSquad]: ...
    async def create_internal_squad(self, *, name: str, inbounds: list[str]) -> RemoteManagedInternalSquad: ...
    async def update_internal_squad(
        self,
        *,
        squad_uuid: str,
        name: str | None = None,
        inbounds: list[str] | None = None,
    ) -> RemoteManagedInternalSquad: ...
    async def healthcheck(self) -> bool: ...
    async def aclose(self) -> None: ...


class RemnawaveClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.remnawave_base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.remnawave_timeout_seconds,
            headers={
                "Authorization": f"Bearer {settings.remnawave_api_token}",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allow_404: bool = False,
        **kwargs: object,
    ) -> object | None:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.settings.remnawave_retry_attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError)),
            reraise=True,
        ):
            with attempt:
                response = await self._client.request(method, path, **kwargs)
                if allow_404 and response.status_code == 404:
                    return None
                response.raise_for_status()
                payload = response.json()
                logger.debug("Remnawave %s %s -> %s", method, path, response.status_code)
                return payload.get("response", payload)
        return None

    async def healthcheck(self) -> bool:
        try:
            response = await self._request("GET", "/api/system/health")
            return bool(response)
        except Exception:
            logger.exception("Remnawave healthcheck failed")
            return False

    async def list_nodes(self) -> list[RemoteNode]:
        payload = await self._request("GET", "/api/nodes")
        if isinstance(payload, dict):
            items = payload.get("nodes") or payload.get("items") or payload.get("data") or []
        else:
            items = payload or []
        return [RemoteNode.model_validate(item) for item in items]

    async def list_users(self) -> list[RemoteUser]:
        users: list[RemoteUser] = []
        start = 0
        size = 200
        while True:
            payload = await self._request("GET", "/api/users", params={"start": start, "size": size})
            if not isinstance(payload, dict):
                break
            batch = [RemoteUser.model_validate(item) for item in payload.get("users", [])]
            total = int(payload.get("total", len(batch)))
            users.extend(batch)
            start += len(batch)
            if not batch or start >= total:
                break
        return users

    async def get_user(self, user_uuid: str) -> RemoteUser:
        payload = await self._request("GET", f"/api/users/{user_uuid}")
        return RemoteUser.model_validate(payload)

    async def find_user_by_telegram_id(self, telegram_id: int) -> RemoteUser | None:
        payload = await self._request("GET", f"/api/users/by-telegram-id/{telegram_id}", allow_404=True)
        if not payload:
            return None
        users = [RemoteUser.model_validate(item) for item in payload]
        return users[0] if users else None

    async def create_user(self, payload: dict) -> RemoteUser:
        response = await self._request("POST", "/api/users", json=payload)
        return RemoteUser.model_validate(response)

    async def update_user(self, payload: dict) -> RemoteUser:
        response = await self._request("PATCH", "/api/users", json=payload)
        return RemoteUser.model_validate(response)

    async def enable_user(self, user_uuid: str) -> RemoteUser:
        payload = await self._request("POST", f"/api/users/{user_uuid}/actions/enable")
        return RemoteUser.model_validate(payload)

    async def disable_user(self, user_uuid: str) -> RemoteUser:
        payload = await self._request("POST", f"/api/users/{user_uuid}/actions/disable")
        return RemoteUser.model_validate(payload)

    async def reset_user_traffic(self, user_uuid: str) -> RemoteUser:
        payload = await self._request("POST", f"/api/users/{user_uuid}/actions/reset-traffic")
        return RemoteUser.model_validate(payload)

    async def revoke_user_subscription(self, user_uuid: str) -> RemoteUser:
        payload = await self._request("POST", f"/api/users/{user_uuid}/actions/revoke")
        return RemoteUser.model_validate(payload)

    async def delete_user(self, user_uuid: str) -> dict:
        payload = await self._request("DELETE", f"/api/users/{user_uuid}")
        return payload if isinstance(payload, dict) else {}

    async def get_accessible_nodes(self, user_uuid: str) -> list[RemoteAccessibleNode]:
        payload = await self._request("GET", f"/api/users/{user_uuid}/accessible-nodes")
        active_nodes = (payload or {}).get("activeNodes", [])
        return [RemoteAccessibleNode.model_validate(item) for item in active_nodes]

    async def get_subscription_info(self, short_uuid: str) -> RemoteSubscriptionInfo:
        payload = await self._request("GET", f"/api/sub/{short_uuid}/info")
        return RemoteSubscriptionInfo.model_validate(payload)

    async def get_connection_keys(self, user_uuid: str) -> RemoteConnectionKeys:
        payload = await self._request("GET", f"/api/subscriptions/connection-keys/{user_uuid}")
        return RemoteConnectionKeys.model_validate(payload)

    async def get_user_hwid_devices(self, user_uuid: str) -> list[RemoteHwidDevice]:
        payload = await self._request("GET", f"/api/hwid/devices/{user_uuid}")
        devices = payload.get("devices", []) if isinstance(payload, dict) else []
        return [RemoteHwidDevice.model_validate(item) for item in devices]

    async def delete_user_hwid_device(self, user_uuid: str, hwid: str) -> list[RemoteHwidDevice]:
        payload = await self._request(
            "POST",
            "/api/hwid/devices/delete",
            json={"userUuid": user_uuid, "hwid": hwid},
        )
        devices = payload.get("devices", []) if isinstance(payload, dict) else []
        return [RemoteHwidDevice.model_validate(item) for item in devices]

    async def fetch_node_users_ips(self, node_uuid: str) -> str:
        payload = await self._request("POST", f"/api/ip-control/fetch-users-ips/{node_uuid}")
        return str((payload or {}).get("jobId") or "")

    async def get_node_users_ips_result(self, job_id: str) -> RemoteNodeUsersIpsJob:
        payload = await self._request("GET", f"/api/ip-control/fetch-users-ips/result/{job_id}")
        return RemoteNodeUsersIpsJob.model_validate(payload)

    async def get_user_usage(self, user_uuid: str, start: date, end: date) -> RemoteUsageResponse:
        payload = await self._request(
            "GET",
            f"/api/bandwidth-stats/users/{user_uuid}",
            params={"start": start.isoformat(), "end": end.isoformat(), "topNodesLimit": 20},
        )
        return RemoteUsageResponse.model_validate(payload)

    async def get_node_user_usage(self, node_uuid: str, start: datetime, end: datetime) -> list[RemoteNodeUserUsageRow] | None:
        payload = await self._request(
            "GET",
            f"/api/bandwidth-stats/nodes/{node_uuid}/users/legacy",
            params={"start": start.isoformat(), "end": end.isoformat()},
            allow_404=True,
        )
        if payload is None:
            payload = await self._request(
                "GET",
                f"/api/nodes/usage/{node_uuid}/users/range",
                params={"start": start.isoformat(), "end": end.isoformat()},
                allow_404=True,
            )
        if payload is None:
            return None
        return [RemoteNodeUserUsageRow.model_validate(item) for item in (payload or [])]

    async def get_subscription_request_history(self, user_uuid: str) -> list[RemoteSubscriptionRequestRecord]:
        payload = await self._request("GET", f"/api/users/{user_uuid}/subscription-request-history")
        records = (payload or {}).get("records", [])
        return [RemoteSubscriptionRequestRecord.model_validate(item) for item in records]

    async def list_internal_squads(self) -> list[RemoteManagedInternalSquad]:
        payload = await self._request("GET", "/api/internal-squads")
        items = (payload or {}).get("internalSquads", [])
        return [RemoteManagedInternalSquad.model_validate(item) for item in items]

    async def create_internal_squad(self, *, name: str, inbounds: list[str]) -> RemoteManagedInternalSquad:
        payload = await self._request("POST", "/api/internal-squads", json={"name": name, "inbounds": inbounds})
        return RemoteManagedInternalSquad.model_validate(payload)

    async def update_internal_squad(
        self,
        *,
        squad_uuid: str,
        name: str | None = None,
        inbounds: list[str] | None = None,
    ) -> RemoteManagedInternalSquad:
        body: dict[str, object] = {"uuid": squad_uuid}
        if name is not None:
            body["name"] = name
        if inbounds is not None:
            body["inbounds"] = inbounds
        payload = await self._request("PATCH", "/api/internal-squads", json=body)
        return RemoteManagedInternalSquad.model_validate(payload)


@asynccontextmanager
async def remnawave_client_scope(settings: Settings) -> AsyncIterator[RemnawaveClient]:
    client = RemnawaveClient(settings)
    try:
        yield client
    finally:
        await client.aclose()
