from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from altlink.application.services.registry import AppContainer
from altlink.logging_config import configure_logging
from altlink.presentation.bots.common import heartbeat_loop
from altlink.scheduler.jobs import billing_job, notifications_job, online_job, sync_servers_job, traffic_job
from altlink.settings import get_settings


async def run_scheduler() -> None:
    settings = get_settings()
    configure_logging(settings.debug)
    container = AppContainer(settings)
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(sync_servers_job, "interval", minutes=settings.sync_servers_interval_minutes, args=[container])
    scheduler.add_job(billing_job, "interval", minutes=settings.billing_interval_minutes, args=[container])
    scheduler.add_job(traffic_job, "interval", minutes=settings.traffic_snapshot_interval_minutes, args=[container])
    scheduler.add_job(
        notifications_job,
        "interval",
        minutes=settings.notification_dispatch_interval_minutes,
        args=[container],
    )
    scheduler.add_job(online_job, "interval", minutes=settings.online_refresh_interval_minutes, args=[container])
    scheduler.start()

    heartbeat = asyncio.create_task(heartbeat_loop("/tmp/altlink-scheduler.heartbeat"))
    try:
        await sync_servers_job(container)
        await billing_job(container)
        await notifications_job(container)
        while True:
            await asyncio.sleep(3600)
    finally:
        heartbeat.cancel()
        scheduler.shutdown(wait=False)
        await container.close()


if __name__ == "__main__":
    asyncio.run(run_scheduler())

