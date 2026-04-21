from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from altlink.application.services.accounts import AccountService
from altlink.application.services.billing import BillingService
from altlink.application.services.catalog import CatalogService
from altlink.application.services.dashboard import DashboardService
from altlink.application.services.notifications import NotificationService
from altlink.application.services.online import OnlineService
from altlink.application.services.topups import TopupService
from altlink.db import create_engine, session_scope
from altlink.infrastructure.remnawave_client import RemnawaveClient, RemnawaveGateway
from altlink.settings import Settings


class ServiceHub:
    def __init__(self, session: AsyncSession, settings: Settings, remnawave: RemnawaveGateway) -> None:
        self.session = session
        self.settings = settings
        self.notifications = NotificationService(session, settings, remnawave)
        self.accounts = AccountService(session, settings, remnawave)
        self.catalog = CatalogService(session, settings, remnawave)
        self.online = OnlineService(session, settings, remnawave)
        self.billing = BillingService(
            session=session,
            settings=settings,
            remnawave=remnawave,
            accounts=self.accounts,
            catalog=self.catalog,
            notifications=self.notifications,
        )
        self.topups = TopupService(
            session=session,
            settings=settings,
            remnawave=remnawave,
            accounts=self.accounts,
            notifications=self.notifications,
        )
        self.dashboard = DashboardService(session, settings, remnawave)


class AppContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine: AsyncEngine = create_engine(settings)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.remnawave = RemnawaveClient(settings)

    @asynccontextmanager
    async def session(self):
        async with session_scope(self.session_factory) as session:
            yield session

    @asynccontextmanager
    async def hub(self):
        async with self.session() as session:
            yield ServiceHub(session, self.settings, self.remnawave)

    async def close(self) -> None:
        await self.remnawave.aclose()
        await self.engine.dispose()
