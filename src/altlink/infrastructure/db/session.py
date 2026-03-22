from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from altlink.settings import Settings


def create_engine_and_factory(settings: Settings) -> tuple:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, session_factory


@asynccontextmanager
async def session_scope(session_factory: async_sessionmaker[AsyncSession]):
    session = session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
