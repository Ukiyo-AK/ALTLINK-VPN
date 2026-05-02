from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from altlink.application.services.registry import AppContainer
from altlink.logging_config import configure_logging
from altlink.presentation.bots.common import heartbeat_loop
from altlink.scheduler.jobs import (
    billing_job,
    notifications_job,
    online_job,
    remnawave_health_job,
    server_latency_job,
    sync_servers_job,
    topups_job,
    traffic_job,
)
from altlink.settings import get_settings

logger = logging.getLogger(__name__)


async def run_startup_job(job, container: AppContainer, job_name: str) -> None:
    try:
        await job(container)
    except Exception:  # noqa: BLE001
        logger.exception("Startup job %s failed", job_name)


async def run_scheduler() -> None:
    settings = get_settings()
    configure_logging(settings.debug)
    container = AppContainer(settings)
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        remnawave_health_job,
        "interval",
        minutes=settings.remnawave_healthcheck_interval_minutes,
        args=[container],
    )
    scheduler.add_job(sync_servers_job, "interval", minutes=settings.sync_servers_interval_minutes, args=[container])
    scheduler.add_job(billing_job, "interval", minutes=settings.billing_interval_minutes, args=[container])
    scheduler.add_job(traffic_job, "interval", minutes=settings.traffic_snapshot_interval_minutes, args=[container])
    scheduler.add_job(
        server_latency_job,
        "interval",
        minutes=settings.server_latency_monitor_interval_minutes,
        args=[container],
    )
    scheduler.add_job(
        notifications_job,
        "interval",
        minutes=settings.notification_dispatch_interval_minutes,
        args=[container],
    )
    scheduler.add_job(
        topups_job,
        "interval",
        minutes=settings.notification_dispatch_interval_minutes,
        args=[container],
    )
    scheduler.add_job(online_job, "interval", minutes=settings.online_refresh_interval_minutes, args=[container])
    scheduler.start()

    heartbeat = asyncio.create_task(heartbeat_loop("/tmp/altlink-scheduler.heartbeat"))
    try:
        await run_startup_job(remnawave_health_job, container, "remnawave_health")
        await run_startup_job(sync_servers_job, container, "sync_servers")
        await run_startup_job(server_latency_job, container, "server_latency")
        await run_startup_job(billing_job, container, "billing")
        await run_startup_job(traffic_job, container, "traffic")
        await run_startup_job(topups_job, container, "topups")
        await run_startup_job(notifications_job, container, "notifications")
        while True:
            await asyncio.sleep(3600)
    finally:
        heartbeat.cancel()
        scheduler.shutdown(wait=False)
        await container.close()


if __name__ == "__main__":
    asyncio.run(run_scheduler())
