from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage

from altlink.core.heartbeat import touch_heartbeat
from altlink.core.logging import configure_logging
from altlink.infrastructure.db import models  # noqa: F401
from altlink.infrastructure.db.session import create_engine_and_factory
from altlink.infrastructure.remnawave import RemnawaveClient
from altlink.presentation.bots.client.handlers import router
from altlink.presentation.bots.common.context import BotContext
from altlink.settings import get_settings


async def heartbeat_loop(path: str, interval_seconds: int) -> None:
    while True:
        touch_heartbeat(path)
        await asyncio.sleep(interval_seconds)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    engine, session_factory = create_engine_and_factory(settings)
    remnawave = RemnawaveClient(settings)
    bot = Bot(settings.client_bot_token)
    storage = RedisStorage.from_url(settings.redis_url)
    dp = Dispatcher(storage=storage)
    dp.include_router(router)
    context = BotContext(
        settings=settings,
        session_factory=session_factory,
        remnawave=remnawave,
        heartbeat_path="/tmp/altlink-client-bot.heartbeat",
    )
    heartbeat = asyncio.create_task(
        heartbeat_loop(context.heartbeat_path, settings.bot_heartbeat_interval_seconds)
    )
    try:
        await dp.start_polling(bot, bot_context=context)
    finally:
        heartbeat.cancel()
        await bot.session.close()
        await remnawave.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

