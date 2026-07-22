from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import joinedload

from altlink.application.services.base import BaseService
from altlink.domain.enums import SubscriptionStatus, SystemEventLevel
from altlink.infrastructure.db.models import Subscription, SystemSetting, TrafficSnapshot, User
from altlink.utils.time import MOSCOW_TZ, utc_now

logger = logging.getLogger(__name__)


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
    USER_ABUSE_STATUS_KEY = "monitoring.user_abuse_status"
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
                "probe_target_host": probe.get("probe_target_host"),
                "probe_target_port": probe.get("probe_target_port"),
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

    async def capture_user_abuse_state(self) -> list[MonitoringAlert]:
        active_subscriptions = await self._list_active_subscriptions()
        hwid_counts = await self._collect_hwid_device_counts(active_subscriptions)
        checked_at = utc_now()
        checked_at_msk = checked_at.astimezone(MOSCOW_TZ)
        day_started_at = checked_at_msk.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        month_started_at = checked_at_msk.replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).astimezone(UTC)
        traffic_usage = await self._collect_period_traffic_usage(
            [item.user_id for item in active_subscriptions],
            day_started_at=day_started_at,
            month_started_at=month_started_at,
        )
        observations = []
        for subscription in active_subscriptions:
            user = subscription.user
            plan = getattr(subscription, "plan", None)
            hwid_device_count = hwid_counts.get(user.id)
            if hwid_device_count is not None:
                user.hwid_device_count = int(hwid_device_count)
                user.hwid_devices_checked_at = checked_at
            usage = traffic_usage.get(user.id, {})
            observations.append(
                {
                    "user_id": user.id,
                    "telegram_id": user.telegram_id,
                    "username": user.username,
                    "hwid_device_count": hwid_device_count,
                    "device_limit": getattr(plan, "device_limit", None),
                    "daily_traffic_bytes": int(usage.get("daily", 0)),
                    "monthly_traffic_bytes": int(usage.get("monthly", 0)),
                    "daily_traffic_threshold_bytes": int(self.settings.user_abuse_daily_traffic_gb) * 1024**3,
                    "monthly_traffic_threshold_bytes": int(self.settings.user_abuse_monthly_traffic_gb) * 1024**3,
                    "daily_period": checked_at_msk.date().isoformat(),
                    "monthly_period": checked_at_msk.strftime("%Y-%m"),
                }
            )
        alerts = await self.record_user_abuse_state(observations)
        subscriptions_by_user = {item.user_id: item for item in active_subscriptions}
        await asyncio.gather(
            *(
                self._enrich_traffic_alert(
                    alert,
                    subscriptions_by_user.get(str(alert.details.get("user_id") or "")),
                    day_started_at=day_started_at,
                    month_started_at=month_started_at,
                    checked_at=checked_at,
                )
                for alert in alerts
                if alert.kind == "user_traffic_anomaly"
            )
        )
        for alert in alerts:
            if alert.kind != "user_traffic_anomaly":
                continue
            await self.log_event(
                level=SystemEventLevel.WARNING,
                event_type="user_abuse_detected",
                message="Обнаружено аномальное потребление трафика пользователя.",
                payload={"kind": alert.kind, **alert.details},
                subject_user_id=str(alert.details.get("user_id") or "") or None,
            )
        return alerts

    async def record_user_abuse_state(self, observations: list[dict]) -> list[MonitoringAlert]:
        previous = await self._read_setting(self.USER_ABUSE_STATUS_KEY)
        previous_users = previous.get("users", {}) if isinstance(previous, dict) else {}
        checked_at = utc_now().isoformat()
        current_users: dict[str, dict] = {}
        alerts: list[MonitoringAlert] = []

        for observation in observations:
            user_id = str(observation.get("user_id") or "").strip()
            if not user_id:
                continue
            previous_state = previous_users.get(user_id, {})
            details = {
                "user_id": user_id,
                "telegram_id": observation.get("telegram_id"),
                "username": observation.get("username"),
                "checked_at": checked_at,
            }

            device_limit = observation.get("device_limit")
            hwid_device_count = observation.get("hwid_device_count")
            if device_limit is None:
                device_alert_active = False
                hwid_device_count = 0
            elif hwid_device_count is None:
                device_alert_active = bool(previous_state.get("device_alert_active"))
                hwid_device_count = int(previous_state.get("hwid_device_count") or 0)
            else:
                device_limit = int(device_limit)
                hwid_device_count = int(hwid_device_count)
                device_alert_active = hwid_device_count > device_limit
            details.update(
                {
                    "hwid_device_count": hwid_device_count,
                    "device_limit": device_limit,
                    "device_alert_active": device_alert_active,
                }
            )

            daily_traffic_bytes = max(int(observation.get("daily_traffic_bytes") or 0), 0)
            monthly_traffic_bytes = max(int(observation.get("monthly_traffic_bytes") or 0), 0)
            daily_threshold = max(int(observation.get("daily_traffic_threshold_bytes") or 0), 0)
            monthly_threshold = max(int(observation.get("monthly_traffic_threshold_bytes") or 0), 0)
            daily_period = str(observation.get("daily_period") or "")
            monthly_period = str(observation.get("monthly_period") or "")
            daily_alert_active = daily_threshold > 0 and daily_traffic_bytes > daily_threshold
            monthly_alert_active = monthly_threshold > 0 and monthly_traffic_bytes > monthly_threshold
            details.update(
                {
                    "daily_traffic_bytes": daily_traffic_bytes,
                    "monthly_traffic_bytes": monthly_traffic_bytes,
                    "daily_traffic_threshold_bytes": daily_threshold,
                    "monthly_traffic_threshold_bytes": monthly_threshold,
                    "daily_period": daily_period,
                    "monthly_period": monthly_period,
                    "daily_traffic_alert_active": daily_alert_active,
                    "monthly_traffic_alert_active": monthly_alert_active,
                }
            )

            if device_alert_active and (
                not previous_state.get("device_alert_active")
                or int(hwid_device_count) > int(previous_state.get("hwid_device_count") or 0)
            ):
                alerts.append(
                    MonitoringAlert(kind="user_hwid_limit_exceeded", subject=self._user_label(details), details=details)
                )
            new_daily_alert = daily_alert_active and (
                previous_state.get("daily_period") != daily_period
                or not previous_state.get("daily_traffic_alert_active")
            )
            new_monthly_alert = monthly_alert_active and (
                previous_state.get("monthly_period") != monthly_period
                or not previous_state.get("monthly_traffic_alert_active")
            )
            if new_daily_alert or new_monthly_alert:
                alerts.append(
                    MonitoringAlert(kind="user_traffic_anomaly", subject=self._user_label(details), details=details)
                )
            if device_alert_active or daily_alert_active or monthly_alert_active:
                current_users[user_id] = details

        await self._write_setting(
            self.USER_ABUSE_STATUS_KEY,
            {"checked_at": checked_at, "users": current_users},
            description="Последние активные антиабуз-тревоги пользователей.",
        )
        if alerts:
            for alert in alerts:
                if alert.kind == "user_traffic_anomaly":
                    continue
                await self.log_event(
                    level=SystemEventLevel.WARNING,
                    event_type="user_abuse_detected",
                    message="Обнаружена подозрительная активность пользователя.",
                    payload={"kind": alert.kind, **alert.details},
                    subject_user_id=str(alert.details.get("user_id") or "") or None,
                )
        return alerts

    async def _collect_period_traffic_usage(
        self,
        user_ids: list[str],
        *,
        day_started_at: datetime,
        month_started_at: datetime,
    ) -> dict[str, dict[str, int]]:
        if not user_ids:
            return {}
        current = await self._snapshot_lifetime_values(user_ids, latest=True)
        day_baseline = await self._snapshot_lifetime_values(
            user_ids,
            latest=True,
            until=day_started_at,
        )
        month_baseline = await self._snapshot_lifetime_values(
            user_ids,
            latest=True,
            until=month_started_at,
        )
        missing_day = [user_id for user_id in user_ids if user_id not in day_baseline]
        missing_month = [user_id for user_id in user_ids if user_id not in month_baseline]
        if missing_day:
            day_baseline.update(
                await self._snapshot_lifetime_values(missing_day, latest=False, since=day_started_at)
            )
        if missing_month:
            month_baseline.update(
                await self._snapshot_lifetime_values(missing_month, latest=False, since=month_started_at)
            )
        result: dict[str, dict[str, int]] = {}
        for user_id in user_ids:
            current_value = int(current.get(user_id, 0))
            day_value = int(day_baseline.get(user_id, 0))
            month_value = int(month_baseline.get(user_id, 0))
            result[user_id] = {
                "daily": current_value - day_value if current_value >= day_value else current_value,
                "monthly": current_value - month_value if current_value >= month_value else current_value,
            }
        return result

    async def _snapshot_lifetime_values(
        self,
        user_ids: list[str],
        *,
        latest: bool,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, int]:
        aggregate = func.max if latest else func.min
        sample_times_query = select(
            TrafficSnapshot.user_id.label("user_id"),
            aggregate(TrafficSnapshot.created_at).label("sampled_at"),
        ).where(
            TrafficSnapshot.user_id.in_(user_ids),
            TrafficSnapshot.server_id.is_(None),
        )
        if since is not None:
            sample_times_query = sample_times_query.where(TrafficSnapshot.created_at >= since)
        if until is not None:
            sample_times_query = sample_times_query.where(TrafficSnapshot.created_at <= until)
        sample_times = sample_times_query.group_by(TrafficSnapshot.user_id).subquery()
        rows = (
            await self.session.execute(
                select(
                    TrafficSnapshot.user_id,
                    func.max(TrafficSnapshot.lifetime_used_bytes),
                )
                .join(
                    sample_times,
                    and_(
                        sample_times.c.user_id == TrafficSnapshot.user_id,
                        sample_times.c.sampled_at == TrafficSnapshot.created_at,
                    ),
                )
                .group_by(TrafficSnapshot.user_id)
            )
        ).all()
        return {str(user_id): int(value or 0) for user_id, value in rows}

    async def _enrich_traffic_alert(
        self,
        alert: MonitoringAlert,
        subscription: Subscription | None,
        *,
        day_started_at: datetime,
        month_started_at: datetime,
        checked_at: datetime,
    ) -> None:
        if self.remnawave is None or subscription is None or subscription.user is None:
            return
        remote_uuid = getattr(subscription.user, "remnawave_user_uuid", None)
        if not remote_uuid:
            return
        period_started_at = (
            month_started_at
            if alert.details.get("monthly_traffic_alert_active")
            else day_started_at
        )
        try:
            usage, accessible_nodes = await asyncio.gather(
                self.remnawave.get_user_usage(
                    remote_uuid,
                    period_started_at.astimezone(MOSCOW_TZ).date(),
                    checked_at.astimezone(MOSCOW_TZ).date(),
                ),
                self.remnawave.get_accessible_nodes(remote_uuid),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to collect per-server traffic for user_id=%s.", subscription.user_id, exc_info=True)
            alert.details["server_stats_error"] = str(exc)
            return

        stats: dict[str, dict] = {}
        for node in accessible_nodes:
            node_uuid = str(getattr(node, "uuid", "") or "")
            if not node_uuid:
                continue
            stats[node_uuid] = {
                "uuid": node_uuid,
                "name": getattr(node, "nodeName", None) or node_uuid,
                "country_code": getattr(node, "countryCode", None),
                "traffic_bytes": 0,
            }
        for node in [*(getattr(usage, "series", None) or []), *(getattr(usage, "topNodes", None) or [])]:
            node_uuid = str(getattr(node, "uuid", "") or "")
            if not node_uuid:
                continue
            current = stats.setdefault(
                node_uuid,
                {
                    "uuid": node_uuid,
                    "name": getattr(node, "name", None) or node_uuid,
                    "country_code": getattr(node, "countryCode", None),
                    "traffic_bytes": 0,
                },
            )
            current["name"] = getattr(node, "name", None) or current["name"]
            current["country_code"] = getattr(node, "countryCode", None) or current["country_code"]
            current["traffic_bytes"] = max(int(getattr(node, "total", 0) or 0), current["traffic_bytes"])
        alert.details["server_stats_period_started_at"] = period_started_at.isoformat()
        alert.details["server_stats"] = sorted(
            stats.values(),
            key=lambda item: (-int(item["traffic_bytes"]), str(item["name"]).lower()),
        )

    async def _list_active_subscriptions(self) -> list[Subscription]:
        subscriptions = list(
            (
                await self.session.scalars(
                    select(Subscription)
                    .where(
                        Subscription.status.in_(
                            [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE]
                        )
                    )
                    .options(joinedload(Subscription.user), joinedload(Subscription.plan))
                    .order_by(Subscription.created_at.desc())
                )
            ).all()
        )
        result: list[Subscription] = []
        seen_user_ids: set[str] = set()
        for item in subscriptions:
            if item.user_id in seen_user_ids:
                continue
            seen_user_ids.add(item.user_id)
            result.append(item)
        return result

    async def _collect_hwid_device_counts(self, subscriptions: list[Subscription]) -> dict[str, int | None]:
        if self.remnawave is None:
            return {}
        semaphore = asyncio.Semaphore(max(int(self.settings.user_abuse_hwid_fetch_concurrency), 1))

        async def load_count(subscription: Subscription) -> tuple[str, int | None]:
            remote_uuid = getattr(subscription.user, "remnawave_user_uuid", None)
            if not remote_uuid or getattr(subscription.plan, "device_limit", None) is None:
                return subscription.user_id, 0
            try:
                async with semaphore:
                    devices = await self.remnawave.get_user_hwid_devices(remote_uuid)
                return subscription.user_id, len(devices)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to collect HWID devices for user_id=%s.", subscription.user_id, exc_info=True)
                return subscription.user_id, None

        return dict(await asyncio.gather(*(load_count(item) for item in subscriptions)))

    @staticmethod
    def _user_label(details: dict) -> str:
        username = str(details.get("username") or "").strip().lstrip("@")
        if username:
            return f"@{username}"
        return str(details.get("telegram_id") or details.get("user_id") or "неизвестный пользователь")

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
