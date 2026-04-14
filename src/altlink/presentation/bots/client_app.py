from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher

from altlink.application.services.registry import AppContainer
from altlink.logging_config import configure_logging
from altlink.presentation.bots.client_handlers import router
from altlink.presentation.bots.common import heartbeat_loop
from altlink.settings import get_settings


async def run_client_bot() -> None:
    settings = get_settings()
    configure_logging(settings.debug)
    container = AppContainer(settings)
    bot = Bot(token=settings.client_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    heartbeat = asyncio.create_task(heartbeat_loop("/tmp/altlink-client-bot.heartbeat"))
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            container=container,
        )
    finally:
        heartbeat.cancel()
        await bot.session.close()
        await container.close()


if __name__ == "__main__":
    asyncio.run(run_client_bot())

