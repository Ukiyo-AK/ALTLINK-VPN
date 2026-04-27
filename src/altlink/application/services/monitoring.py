from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from altlink.application.services.base import BaseService
from altlink.domain.enums import SystemEventLevel
from altlink.infrastructure.db.models import SystemSetting
from altlink.utils.time import utc_now


@dataclass(slots=True)
class MonitoringAlert:
    kind: str
    subject: str
    details: dict


class MonitoringService(BaseService):
    source = "monitoring"

    REMNAWAVE_STATUS_KEY = "monitoring.remnawave_status"
    SERVER_OPERATIONAL_STATUS_KEY = "monitoring.server_operational_status"
    SERVER_LATENCY_STATUS_KEY = "monitoring.server_latency_status"

    async def is_client_maintenance_active(self) -> bool:
        if not self._remnawave_is_configured():
            return False
        state = await self._read_setting(self.REMNAWAVE_STATUS_KEY)
        if not isinstance(state, dict):
            return False
        return state.get("available") is False

    async def capture_remnawave_status(self, *, error: str | None = None) -> dict:
        now = utc_now().isoformat()
        if not self._remnawave_is_configured() or self.remnawave is None:
            state = {
                "available": True,
                "configured": False,
                "checked_at": now,
                "error": None,
            }
            await self._write_setting(
                self.REMNAWAVE_STATUS_KEY,
                state,
                description="Последний статус доступности Remnawave.",
            )
            return {**state, "became_unavailable": False, "recovered": False}

        available = False
        error_message = error
        if error_message is None:
            available = await self.remnawave.healthcheck()
            if not available:
                error_message = "Healthcheck returned false."

        previous = await self._read_setting(self.REMNAWAVE_STATUS_KEY)
        previous_available = previous.get("available") if isinstance(previous, dict) else None
        became_unavailable = available is False and previous_available is not False
        recovered = available is True and previous_available is False

        state = {
            "available": available,
            "configured": True,
            "checked_at": now,
            "error": error_message,
        }
        await self._write_setting(
            self.REMNAWAVE_STATUS_KEY,
            state,
            description="Последний статус доступности Remnawave.",
        )

        if became_unavailable:
            await self.log_event(
                level=SystemEventLevel.WARNING,
                event_type="remnawave_unavailable",
                message="Remnawave недоступна.",
                payload={"error": error_message},
            )
        elif recovered:
            await self.log_event(
                level=SystemEventLevel.INFO,
                event_type="remnawave_recovered",
                message="Remnawave снова доступна.",
            )

        return {**state, "became_unavailable": became_unavailable, "recovered": recovered}

    async def record_server_operational_state(self, servers) -> list[MonitoringAlert]:
        previous = await self._read_setting(self.SERVER_OPERATIONAL_STATUS_KEY)
        previous_servers = previous.get("servers", {}) if isinstance(previous, dict) else {}

        checked_at = utc_now().isoformat()
        current_servers: dict[str, dict] = {}
        alerts: list[MonitoringAlert] = []
        for server in servers:
            has_active_inbounds = self._server_has_active_inbounds(server)
            operational = bool(server.is_available and server.is_connected and has_active_inbounds)
            reasons: list[str] = []
            if not server.is_available:
                reasons.append("сервер выключен в каталоге")
            if not server.is_connected:
                reasons.append("узел не подключён к Remnawave")
            if not has_active_inbounds:
                reasons.append("нет активных inbound'ов")
            current_servers[server.id] = {
                "name": server.name,
                "address": server.address,
                "country_code": (server.country_code or "").upper(),
                "operational": operational,
                "is_available": bool(server.is_available),
                "is_connected": bool(server.is_connected),
                "has_active_inbounds": has_active_inbounds,
                "reason": "; ".join(reasons) if reasons else None,
                "checked_at": checked_at,
            }
            previous_state = previous_servers.get(server.id)
            was_operational = True if previous_state is None else bool(previous_state.get("operational"))
            if not operational and was_operational:
                alerts.append(
                    MonitoringAlert(
                        kind="server_operational_down",
                        subject=server.name,
                        details=current_servers[server.id],
                    )
                )

        await self._write_setting(
            self.SERVER_OPERATIONAL_STATUS_KEY,
            {"checked_at": checked_at, "servers": current_servers},
            description="Последний оперативный статус доступности серверов.",
        )
        if alerts:
            await self.log_event(
                level=SystemEventLevel.WARNING,
                event_type="server_operational_down",
                message="Один или несколько серверов пропали из доступности.",
                payload={"servers": [item.details for item in alerts]},
            )
        return alerts

    async def record_server_latency_state(self, probes: list[dict]) -> list[MonitoringAlert]:
        previous = await self._read_setting(self.SERVER_LATENCY_STATUS_KEY)
        previous_servers = previous.get("servers", {}) if isinstance(previous, dict) else {}

        checked_at = utc_now().isoformat()
        current_servers: dict[str, dict] = {}
        alerts: list[MonitoringAlert] = []
        for probe in probes:
            server_id = str(probe.get("server_id") or "").strip()
            if not server_id:
                continue
            current_servers[server_id] = {
                "name": probe.get("name"),
                "address": probe.get("address"),
                "country_code": probe.get("country_code"),
                "reachable": bool(probe.get("reachable")),
                "latency_ms": probe.get("latency_ms"),
                "error": probe.get("error") or probe.get("recheck_error"),
                "checked_at": checked_at,
            }
            previous_state = previous_servers.get(server_id)
            was_reachable = True if previous_state is None else bool(previous_state.get("reachable"))
            if not current_servers[server_id]["reachable"] and was_reachable:
                alerts.append(
                    MonitoringAlert(
                        kind="server_latency_down",
                        subject=str(probe.get("name") or server_id),
                        details=current_servers[server_id],
                    )
                )

        await self._write_setting(
            self.SERVER_LATENCY_STATUS_KEY,
            {"checked_at": checked_at, "servers": current_servers},
            description="Последний результат ping-проверки серверов.",
        )
        if alerts:
            await self.log_event(
                level=SystemEventLevel.WARNING,
                event_type="server_latency_down",
                message="Один или несколько серверов не ответили на ping-проверку.",
                payload={"servers": [item.details for item in alerts]},
            )
        return alerts

    async def _read_setting(self, key: str):
        item = await self.session.scalar(select(SystemSetting).where(SystemSetting.key == key))
        return item.value if item is not None else None

    async def _write_setting(self, key: str, value, *, description: str | None = None) -> None:
        item = await self.session.scalar(select(SystemSetting).where(SystemSetting.key == key))
        if item is None:
            item = SystemSetting(key=key, value=value, description=description)
            self.session.add(item)
            return
        item.value = value
        if description and not item.description:
            item.description = description

    def _remnawave_is_configured(self) -> bool:
        return bool((self.settings.remnawave_base_url or "").strip() and (self.settings.remnawave_api_token or "").strip())

    def _server_has_active_inbounds(self, server) -> bool:
        return any(
            getattr(inbound, "is_active", False) and getattr(inbound, "remnawave_inbound_uuid", None)
            for inbound in getattr(server, "inbounds", None) or []
        )
