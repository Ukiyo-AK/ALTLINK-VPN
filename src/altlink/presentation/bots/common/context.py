from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from altlink.infrastructure.remnawave import RemnawaveClient
from altlink.settings import Settings


@dataclass(slots=True)
class BotContext:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    remnawave: RemnawaveClient
    heartbeat_path: str


@asynccontextmanager
async def open_session(bot_context: BotContext):
    session = bot_context.session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()

