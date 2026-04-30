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
    CLIENT_MANUAL_MAINTENANCE_KEY = "monitoring.client_manual_maintenance"

    async def is_client_maintenance_active(self, telegram_id: int | None = None) -> bool:
        manual_state = await self.get_manual_client_maintenance_state()
        if manual_state.get("enabled") and not self._is_manual_maintenance_exception(manual_state, telegram_id):
            return True

        if not self._remnawave_is_configured():
            return False
        state = await self._read_setting(self.REMNAWAVE_STATUS_KEY)
        if not isinstance(state, dict):
            return False
        return state.get("available") is False

    async def get_manual_client_maintenance_state(self) -> dict:
        raw_state = await self._read_setting(self.CLIENT_MANUAL_MAINTENANCE_KEY)
        return self._normalize_manual_maintenance_state(raw_state)

    async def set_manual_client_maintenance(
        self,
        enabled: bool,
        *,
        actor_admin_id: str | None = None,
    ) -> dict:
        state = await self.get_manual_client_maintenance_state()
        if state.get("enabled") == bool(enabled):
            return state

        state["enabled"] = bool(enabled)
        state["updated_at"] = utc_now().isoformat()
        state["updated_by_admin_id"] = actor_admin_id
        await self._write_setting(
            self.CLIENT_MANUAL_MAINTENANCE_KEY,
            state,
            description="Ручной режим технических работ клиентского бота и список исключений.",
        )
        await self.log_event(
            level=SystemEventLevel.WARNING if enabled else SystemEventLevel.INFO,
            event_type="client_manual_maintenance_enabled" if enabled else "client_manual_maintenance_disabled",
            message="Ручной режим технических работ клиентского бота включён."
            if enabled
            else "Ручной режим технических работ клиентского бота отключён.",
            payload={"exceptions": state.get("exceptions", [])},
            actor_admin_id=actor_admin_id,
        )
        return state

    async def add_manual_maintenance_exception(self, user, *, actor_admin_id: str | None = None) -> dict:
        state = await self.get_manual_client_maintenance_state()
        exceptions = state.get("exceptions", [])
        serialized = self._serialize_manual_exception_user(user)
        existing = {str(item.get("user_id")) for item in exceptions}
        if serialized["user_id"] not in existing:
            exceptions.append(serialized)
            exceptions.sort(key=lambda item: str(item.get("telegram_id") or 0))
            state["exceptions"] = exceptions
            state["updated_at"] = utc_now().isoformat()
            state["updated_by_admin_id"] = actor_admin_id
            await self._write_setting(
                self.CLIENT_MANUAL_MAINTENANCE_KEY,
                state,
                description="Ручной режим технических работ клиентского бота и список исключений.",
            )
            await self.log_event(
                level=SystemEventLevel.INFO,
                event_type="client_manual_maintenance_exception_added",
                message="Пользователь добавлен в исключения ручных техработ клиентского бота.",
                payload=serialized,
                actor_admin_id=actor_admin_id,
            )
        return state

    async def remove_manual_maintenance_exception(self, user, *, actor_admin_id: str | None = None) -> dict:
        state = await self.get_manual_client_maintenance_state()
        exceptions = state.get("exceptions", [])
        user_id = str(getattr(user, "id", "") or "")
        filtered = [item for item in exceptions if str(item.get("user_id") or "") != user_id]
        if len(filtered) != len(exceptions):
            state["exceptions"] = filtered
            state["updated_at"] = utc_now().isoformat()
            state["updated_by_admin_id"] = actor_admin_id
            await self._write_setting(
                self.CLIENT_MANUAL_MAINTENANCE_KEY,
                state,
                description="Ручной режим технических работ клиентского бота и список исключений.",
            )
            await self.log_event(
                level=SystemEventLevel.INFO,
                event_type="client_manual_maintenance_exception_removed",
                message="Пользователь удалён из исключений ручных техработ клиентского бота.",
                payload=self._serialize_manual_exception_user(user),
                actor_admin_id=actor_admin_id,
            )
        return state

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

    def _normalize_manual_maintenance_state(self, raw_state) -> dict:
        state = raw_state if isinstance(raw_state, dict) else {}
        exceptions = state.get("exceptions")
        if not isinstance(exceptions, list):
            exceptions = []
        normalized_exceptions = []
        for item in exceptions:
            if not isinstance(item, dict):
                continue
            telegram_id = item.get("telegram_id")
            try:
                telegram_id = int(telegram_id)
            except (TypeError, ValueError):
                continue
            normalized_exceptions.append(
                {
                    "user_id": str(item.get("user_id") or ""),
                    "telegram_id": telegram_id,
                    "username": str(item.get("username") or "").strip() or None,
                    "label": str(item.get("label") or "").strip() or self._exception_label(telegram_id, item.get("username")),
                }
            )
        return {
            "enabled": bool(state.get("enabled")),
            "updated_at": state.get("updated_at"),
            "updated_by_admin_id": state.get("updated_by_admin_id"),
            "exceptions": normalized_exceptions,
        }

    def _serialize_manual_exception_user(self, user) -> dict:
        telegram_id = int(getattr(user, "telegram_id"))
        username = (getattr(user, "username", None) or "").strip() or None
        return {
            "user_id": str(getattr(user, "id")),
            "telegram_id": telegram_id,
            "username": username,
            "label": self._exception_label(telegram_id, username),
        }

    def _exception_label(self, telegram_id: int, username: str | None) -> str:
        if username:
            return f"@{str(username).lstrip('@')}"
        return str(telegram_id)

    def _is_manual_maintenance_exception(self, state: dict, telegram_id: int | None) -> bool:
        if telegram_id is None:
            return False
        for item in state.get("exceptions", []):
            if int(item.get("telegram_id", 0)) == int(telegram_id):
                return True
        return False
