from __future__ import annotations

from altlink.application.services.registry import AppContainer


async def sync_servers_job(container: AppContainer) -> None:
    async with container.hub() as hub:
        await hub.catalog.sync_servers()


async def billing_job(container: AppContainer) -> None:
    async with container.hub() as hub:
        await hub.billing.process_due_subscriptions()


async def traffic_job(container: AppContainer) -> None:
    async with container.hub() as hub:
        await hub.billing.snapshot_traffic()


async def notifications_job(container: AppContainer) -> None:
    async with container.hub() as hub:
        await hub.notifications.dispatch_pending(container.settings.client_bot_token)


async def online_job(container: AppContainer) -> None:
    async with container.hub() as hub:
        await hub.online.refresh_online_cache(detailed=False)

