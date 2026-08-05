from __future__ import annotations

import asyncio

from altlink.application.services.registry import AppContainer
from altlink.infrastructure.db.models import SystemSetting
from altlink.presentation.bots.common import send_telegram_messages
from altlink.utils.latency import (
    LEGACY_WHITELIST_LATENCY_TARGET_SETTING_KEY,
    WHITELIST_SERVER_DOMAIN_SETTING_KEY,
    is_whitelist_latency_target,
    normalize_latency_target_domain,
    probe_server_latency,
)
from sqlalchemy import select


def format_remnawave_alert(state: dict) -> str:
    lines = [
        "Тревога: Remnawave недоступна",
        "",
        "Клиентский бот переведён в режим технических работ до восстановления панели.",
    ]
    error = (state or {}).get("error")
    if error:
        lines.extend(["", f"Причина: {error}"])
    return "\n".join(lines)


def format_remnawave_recovery_alert() -> str:
    return "Remnawave снова доступна. Режим технических работ для клиентского бота снят."


def format_server_operational_alert(alerts) -> str:
    lines = ["Тревога: серверы пропали из доступности", ""]
    for item in alerts:
        details = item.details
        reason = details.get("reason") or "сервер недоступен"
        country = details.get("country_code") or "—"
        lines.append(f"• {item.subject} [{country}] — {reason}")
        lines.append(f"  Адрес: {details.get('address') or '—'}")
    return "\n".join(lines)


def format_server_latency_alert(alerts) -> str:
    lines = ["Тревога: серверы не ответили на ping-проверку", ""]
    for item in alerts:
        details = item.details
        country = details.get("country_code") or "—"
        error = details.get("error") or "нет ответа"
        lines.append(f"• {item.subject} [{country}] — {error}")
        lines.append(f"  Адрес: {details.get('address') or '—'}")
        target_host = details.get("probe_target_host")
        target_port = details.get("probe_target_port")
        if target_host:
            lines.append(f"  Цель проверки: {target_host}:{target_port or '—'}")
    return "\n".join(lines)


def format_user_abuse_alert(alerts) -> str:
    lines = ["⚠️ Антиабуз: обнаружена подозрительная активность", ""]
    for item in alerts:
        details = item.details
        telegram_id = details.get("telegram_id") or "—"
        lines.append(f"• {item.subject} (Telegram ID: {telegram_id})")
        if item.kind == "user_many_active_ips":
            lines.append(
                f"  Одновременно подключено уникальных IP: {details.get('unique_ip_count', 0)} "
                f"(порог: {details.get('unique_ip_threshold', '—')})"
            )
            ips = details.get("unique_ips") or []
            if ips:
                lines.append(f"  IP: {', '.join(str(ip) for ip in ips[:12])}")
        elif item.kind == "user_hwid_limit_exceeded":
            lines.append(
                f"  HWID-устройств: {details.get('hwid_device_count', 0)} "
                f"при лимите {details.get('device_limit', '—')}"
            )
        elif item.kind == "user_traffic_anomaly":
            daily_bytes = int(details.get("daily_traffic_bytes") or 0)
            monthly_bytes = int(details.get("monthly_traffic_bytes") or 0)
            if details.get("daily_traffic_alert_active"):
                daily_threshold = int(details.get("daily_traffic_threshold_bytes") or 0) / 1024**3
                lines.append(f"  За сутки: {daily_bytes / 1024**3:.2f} ГБ (порог: {daily_threshold:g} ГБ)")
            if details.get("monthly_traffic_alert_active"):
                monthly_threshold = int(details.get("monthly_traffic_threshold_bytes") or 0) / 1024**4
                lines.append(f"  За месяц: {monthly_bytes / 1024**4:.2f} ТБ (порог: {monthly_threshold:g} ТБ)")
            server_stats = details.get("server_stats") or []
            if server_stats:
                lines.append("  Статистика по серверам:")
                for server in server_stats:
                    country = server.get("country_code") or "—"
                    traffic_gb = int(server.get("traffic_bytes") or 0) / 1024**3
                    lines.append(f"    • {server.get('name') or server.get('uuid')} [{country}]: {traffic_gb:.2f} ГБ")
            elif details.get("server_stats_error"):
                lines.append("  Статистику по серверам временно получить не удалось.")
    return "\n".join(lines)


def split_telegram_text(text: str, *, limit: int = 3900) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in text.splitlines():
        added_length = len(line) + (1 if current else 0)
        if current and current_length + added_length > limit:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks or [text]


async def sync_servers_job(container: AppContainer) -> None:
    admin_ids: list[int] = []
    alerts = []
    async with container.hub() as hub:
        servers = await hub.catalog.sync_servers()
        alerts = await hub.monitoring.record_server_operational_state(servers)
        if alerts:
            admin_ids = await hub.accounts.list_admin_telegram_ids()
    if alerts:
        await send_telegram_messages(
            bot_token=container.settings.admin_bot_token,
            chat_ids=admin_ids,
            text=format_server_operational_alert(alerts),
        )


async def server_failover_job(container: AppContainer) -> None:
    admin_ids: list[int] = []
    alerts = []
    async with container.hub() as hub:
        await hub.catalog.refresh_server_health_and_failover()
        servers = await hub.catalog.list_servers()
        alerts = await hub.monitoring.record_server_operational_state(servers)
        if alerts:
            admin_ids = await hub.accounts.list_admin_telegram_ids()
    if alerts:
        await send_telegram_messages(
            bot_token=container.settings.admin_bot_token,
            chat_ids=admin_ids,
            text=format_server_operational_alert(alerts),
        )


async def billing_job(container: AppContainer) -> None:
    async with container.hub() as hub:
        await hub.billing.process_due_subscriptions()


async def traffic_job(container: AppContainer) -> None:
    async with container.hub() as hub:
        await hub.billing.snapshot_traffic()


async def notifications_job(container: AppContainer) -> None:
    async with container.hub() as hub:
        await hub.notifications.dispatch_pending(container.settings.client_bot_token)


async def topups_job(container: AppContainer) -> None:
    async with container.hub() as hub:
        await hub.topups.sync_pending_yookassa_checkouts()


async def online_job(container: AppContainer) -> None:
    async with container.hub() as hub:
        await hub.online.refresh_online_cache(detailed=False)


async def hwid_device_cleanup_job(container: AppContainer) -> None:
    async with container.hub() as hub:
        await hub.accounts.cleanup_inactive_hwid_devices(
            inactive_days=container.settings.hwid_device_inactive_days,
            concurrency=container.settings.hwid_device_cleanup_concurrency,
        )


async def user_abuse_monitor_job(container: AppContainer) -> None:
    admin_ids: list[int] = []
    alerts = []
    async with container.hub() as hub:
        alerts = await hub.monitoring.capture_user_abuse_state()
        alerts = [item for item in alerts if item.kind != "user_many_active_ips"]
        if alerts:
            admin_ids = await hub.accounts.list_admin_telegram_ids()
    if alerts:
        for alert in alerts:
            for message_part in split_telegram_text(format_user_abuse_alert([alert])):
                await send_telegram_messages(
                    bot_token=container.settings.admin_bot_token,
                    chat_ids=admin_ids,
                    text=message_part,
                )


async def remnawave_health_job(container: AppContainer) -> None:
    admin_ids: list[int] = []
    alert_text: str | None = None
    async with container.hub() as hub:
        state = await hub.monitoring.capture_remnawave_status()
        if state.get("became_unavailable") or state.get("recovered"):
            admin_ids = await hub.accounts.list_admin_telegram_ids()
            alert_text = (
                format_remnawave_alert(state)
                if state.get("became_unavailable")
                else format_remnawave_recovery_alert()
            )
    if alert_text:
        await send_telegram_messages(
            bot_token=container.settings.admin_bot_token,
            chat_ids=admin_ids,
            text=alert_text,
        )


async def server_latency_job(container: AppContainer) -> None:
    admin_ids: list[int] = []
    alerts = []
    async with container.hub() as hub:
        servers = await hub.catalog.list_servers()
        settings_items = list(
            (
                await hub.session.scalars(
                    select(SystemSetting).where(
                        SystemSetting.key.in_(
                            [
                                WHITELIST_SERVER_DOMAIN_SETTING_KEY,
                                LEGACY_WHITELIST_LATENCY_TARGET_SETTING_KEY,
                            ]
                        )
                    )
                )
            ).all()
        )
        settings_by_key = {item.key: item for item in settings_items}
        setting = settings_by_key.get(WHITELIST_SERVER_DOMAIN_SETTING_KEY) or settings_by_key.get(
            LEGACY_WHITELIST_LATENCY_TARGET_SETTING_KEY
        )
        whitelist_server_domain = normalize_latency_target_domain(getattr(setting, "value", None))
        targets = [
            server
            for server in servers
            if getattr(server, "is_available", False)
            and getattr(server, "is_connected", False)
            and any(
                getattr(inbound, "is_active", False) and getattr(inbound, "remnawave_inbound_uuid", None)
                for inbound in getattr(server, "inbounds", None) or []
            )
        ]
        raw_probes = await asyncio.gather(
            *(
                probe_server_latency(
                    server,
                    override_host=(
                        whitelist_server_domain
                        if whitelist_server_domain and is_whitelist_latency_target(server)
                        else None
                    ),
                )
                for server in targets
            )
        )
        probes = [
            {
                **probe,
                "server_id": getattr(server, "id", None),
                "address": getattr(server, "address", None),
            }
            for server, probe in zip(targets, raw_probes)
        ]
        alerts = await hub.monitoring.record_server_latency_state(probes)
        if alerts:
            admin_ids = await hub.accounts.list_admin_telegram_ids()
    if alerts:
        await send_telegram_messages(
            bot_token=container.settings.admin_bot_token,
            chat_ids=admin_ids,
            text=format_server_latency_alert(alerts),
        )
