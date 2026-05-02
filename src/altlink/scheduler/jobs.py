from __future__ import annotations

import asyncio

from altlink.application.services.registry import AppContainer
from altlink.presentation.bots.common import send_telegram_messages
from altlink.utils.latency import probe_server_latency


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
    return "\n".join(lines)


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
        raw_probes = await asyncio.gather(*(probe_server_latency(server) for server in targets))
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
