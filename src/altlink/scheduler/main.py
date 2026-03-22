from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from altlink.application.services import BillingService, NotificationService, ServerService, TrafficService
from altlink.core.heartbeat import touch_heartbeat
from altlink.core.logging import configure_logging
from altlink.infrastructure.db import models  # noqa: F401
from altlink.infrastructure.db.session import create_engine_and_factory
from altlink.infrastructure.remnawave import RemnawaveClient
from altlink.settings import get_settings

logger = logging.getLogger("scheduler")


async def heartbeat_loop(path: str, interval_seconds: int) -> None:
    while True:
        touch_heartbeat(path)
        await asyncio.sleep(interval_seconds)


async def run_job(name: str, session_factory, settings, remnawave) -> None:
    async with session_factory() as session:
        try:
            if name == "billing_due":
                await BillingService(session, settings, remnawave).process_due_subscriptions()
            elif name == "billing_notifications":
                await BillingService(session, settings, remnawave).queue_prebilling_and_low_balance_notifications()
            elif name == "notification_dispatch":
                await NotificationService(session, settings, remnawave).send_due_notifications()
            elif name == "server_sync":
                await ServerService(session, settings, remnawave).sync_from_remnawave()
            elif name == "traffic_sync":
                await TrafficService(session, settings, remnawave).sync_active_user_traffic()
            elif name == "online_sync":
                await TrafficService(session, settings, remnawave).sync_online_sessions()
            await session.commit()
            logger.info("Scheduler job finished", extra={"job": name})
        except Exception:  # noqa: BLE001
            await session.rollback()
            logger.exception("Scheduler job failed", extra={"job": name})


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    engine, session_factory = create_engine_and_factory(settings)
    remnawave = RemnawaveClient(settings)

    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(run_job, "interval", minutes=5, args=["billing_due", session_factory, settings, remnawave], id="billing_due", replace_existing=True)
    scheduler.add_job(run_job, "interval", hours=6, args=["billing_notifications", session_factory, settings, remnawave], id="billing_notifications", replace_existing=True)
    scheduler.add_job(run_job, "interval", minutes=2, args=["notification_dispatch", session_factory, settings, remnawave], id="notification_dispatch", replace_existing=True)
    scheduler.add_job(run_job, "interval", hours=1, args=["server_sync", session_factory, settings, remnawave], id="server_sync", replace_existing=True)
    scheduler.add_job(run_job, "interval", minutes=15, args=["traffic_sync", session_factory, settings, remnawave], id="traffic_sync", replace_existing=True)
    scheduler.add_job(run_job, "interval", minutes=5, args=["online_sync", session_factory, settings, remnawave], id="online_sync", replace_existing=True)
    scheduler.start()

    heartbeat = asyncio.create_task(
        heartbeat_loop("/tmp/altlink-scheduler.heartbeat", settings.bot_heartbeat_interval_seconds)
    )
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        heartbeat.cancel()
        scheduler.shutdown(wait=False)
        await remnawave.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
