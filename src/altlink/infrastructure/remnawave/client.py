from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from altlink.infrastructure.remnawave.exceptions import (
    RemnawaveNotFoundError,
    RemnawaveRequestError,
)
from altlink.infrastructure.remnawave.schemas import (
    RemnawaveAccessibleNodes,
    RemnawaveConnectionKeys,
    RemnawaveInboundWithSquads,
    RemnawaveNode,
    RemnawaveNodeMetric,
    RemnawaveRealtimeNodeUsage,
    RemnawaveSubscriptionInfo,
    RemnawaveSubscriptionRequestHistory,
    RemnawaveSystemStats,
    RemnawaveUser,
    RemnawaveUserUsage,
)
from altlink.settings import Settings


class RemnawaveClient:
    """Official API adapter.

    Confirmed against official Remnawave backend routes:
    /api/users
    /api/nodes
    /api/config-profiles/inbounds
    /api/subscriptions
    /api/bandwidth-stats
    /api/system
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(self.__class__.__name__)
        self._client = httpx.AsyncClient(
            base_url=settings.remwave_base_url.rstrip("/"),
            timeout=settings.remwave_timeout_seconds,
            verify=settings.remwave_verify_tls,
            headers={
                "Authorization": f"Bearer {settings.remwave_api_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type(RemnawaveRequestError),
    )
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            self.logger.exception("Ошибка запроса к Remnawave", extra={"path": path})
            raise RemnawaveRequestError(str(exc)) from exc

        if response.status_code == 404:
            raise RemnawaveNotFoundError(f"Remnawave resource not found: {path}")
        if response.status_code >= 500:
            self.logger.error(
                "Временная ошибка Remnawave",
                extra={"status_code": response.status_code, "path": path, "body": response.text[:500]},
            )
            raise RemnawaveRequestError(f"Temporary Remnawave error {response.status_code}")
        if response.status_code >= 400:
            self.logger.error(
                "Ошибка Remnawave",
                extra={"status_code": response.status_code, "path": path, "body": response.text[:500]},
            )
            raise RemnawaveRequestError(f"Remnawave error {response.status_code}: {response.text}")

        return response.json()

    async def list_nodes(self) -> list[RemnawaveNode]:
        payload = await self._request("GET", "/api/nodes")
        return [RemnawaveNode.model_validate(item) for item in payload["response"]]

    async def list_all_inbounds(self) -> list[RemnawaveInboundWithSquads]:
        payload = await self._request("GET", "/api/config-profiles/inbounds")
        return [RemnawaveInboundWithSquads.model_validate(item) for item in payload["response"]["inbounds"]]

    async def create_user(
        self,
        *,
        username: str,
        expire_at: datetime,
        traffic_limit_bytes: int | None,
        telegram_id: int,
        description: str | None = None,
    ) -> RemnawaveUser:
        body = {
            "username": username,
            "status": "ACTIVE",
            "expireAt": expire_at.isoformat(),
            "trafficLimitBytes": traffic_limit_bytes or 0,
            "trafficLimitStrategy": "NO_RESET",
            "telegramId": telegram_id,
            "description": description,
        }
        payload = await self._request("POST", "/api/users", json=body)
        return RemnawaveUser.model_validate(payload["response"])

    async def update_user(
        self,
        *,
        uuid: str,
        expire_at: datetime | None = None,
        traffic_limit_bytes: int | None = None,
        description: str | None = None,
        telegram_id: int | None = None,
        status: str | None = None,
    ) -> RemnawaveUser:
        body: dict[str, Any] = {"uuid": uuid}
        if expire_at is not None:
            body["expireAt"] = expire_at.isoformat()
        if traffic_limit_bytes is not None:
            body["trafficLimitBytes"] = traffic_limit_bytes
            body["trafficLimitStrategy"] = "NO_RESET"
        if description is not None:
            body["description"] = description
        if telegram_id is not None:
            body["telegramId"] = telegram_id
        if status is not None:
            body["status"] = status
        payload = await self._request("PATCH", "/api/users", json=body)
        return RemnawaveUser.model_validate(payload["response"])

    async def get_user_by_uuid(self, uuid: str) -> RemnawaveUser:
        payload = await self._request("GET", f"/api/users/{uuid}")
        return RemnawaveUser.model_validate(payload["response"])

    async def get_users_by_telegram_id(self, telegram_id: int) -> list[RemnawaveUser]:
        payload = await self._request("GET", f"/api/users/by-telegram-id/{telegram_id}")
        return [RemnawaveUser.model_validate(item) for item in payload["response"]]

    async def enable_user(self, uuid: str) -> RemnawaveUser:
        payload = await self._request("POST", f"/api/users/{uuid}/actions/enable")
        return RemnawaveUser.model_validate(payload["response"])

    async def disable_user(self, uuid: str) -> RemnawaveUser:
        payload = await self._request("POST", f"/api/users/{uuid}/actions/disable")
        return RemnawaveUser.model_validate(payload["response"])

    async def reset_user_traffic(self, uuid: str) -> RemnawaveUser:
        payload = await self._request("POST", f"/api/users/{uuid}/actions/reset-traffic")
        return RemnawaveUser.model_validate(payload["response"])

    async def get_user_accessible_nodes(self, uuid: str) -> RemnawaveAccessibleNodes:
        payload = await self._request("GET", f"/api/users/{uuid}/accessible-nodes")
        return RemnawaveAccessibleNodes.model_validate(payload["response"])

    async def get_subscription_info(self, uuid: str) -> RemnawaveSubscriptionInfo:
        payload = await self._request("GET", f"/api/subscriptions/by-uuid/{uuid}")
        return RemnawaveSubscriptionInfo.model_validate(payload["response"])

    async def get_connection_keys(self, uuid: str) -> RemnawaveConnectionKeys:
        payload = await self._request("GET", f"/api/subscriptions/connection-keys/{uuid}")
        return RemnawaveConnectionKeys.model_validate(payload["response"])

    async def get_user_usage(
        self,
        uuid: str,
        *,
        start: date,
        end: date,
        top_nodes_limit: int = 20,
    ) -> RemnawaveUserUsage:
        payload = await self._request(
            "GET",
            f"/api/bandwidth-stats/users/{uuid}",
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "topNodesLimit": top_nodes_limit,
            },
        )
        return RemnawaveUserUsage.model_validate(payload["response"])

    async def get_user_subscription_request_history(self, uuid: str) -> RemnawaveSubscriptionRequestHistory:
        payload = await self._request("GET", f"/api/users/{uuid}/subscription-request-history")
        return RemnawaveSubscriptionRequestHistory.model_validate(payload["response"])

    async def get_system_stats(self) -> RemnawaveSystemStats:
        payload = await self._request("GET", "/api/system/stats")
        return RemnawaveSystemStats.model_validate(payload["response"])

    async def get_nodes_realtime_usage(self) -> list[RemnawaveRealtimeNodeUsage]:
        payload = await self._request("GET", "/api/bandwidth-stats/nodes/realtime")
        return [RemnawaveRealtimeNodeUsage.model_validate(item) for item in payload["response"]]

    async def get_nodes_metrics(self) -> list[RemnawaveNodeMetric]:
        payload = await self._request("GET", "/api/system/nodes/metrics")
        return [RemnawaveNodeMetric.model_validate(item) for item in payload["response"]["nodes"]]

