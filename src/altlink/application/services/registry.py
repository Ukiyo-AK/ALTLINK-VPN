from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from altlink.application.services.accounts import AccountService
from altlink.application.services.backups import BackupService
from altlink.application.services.billing import BillingService
from altlink.application.services.catalog import CatalogService
from altlink.application.services.dashboard import DashboardService
from altlink.application.services.external_api import ExternalApiService
from altlink.application.services.monitoring import MonitoringService
from altlink.application.services.notifications import NotificationService
from altlink.application.services.online import OnlineService
from altlink.application.services.portal_auth import PortalAuthService
from altlink.application.services.promos import PromoService
from altlink.application.services.support import SupportService
from altlink.application.services.topups import TopupService
from altlink.db import create_engine, ensure_runtime_schema, session_scope
from altlink.domain.plans import DEFAULT_PLAN_SEEDS
from altlink.infrastructure.db.models import Plan
from altlink.infrastructure.remnawave_client import RemnawaveClient, RemnawaveGateway
from altlink.settings import Settings


class ServiceHub:
    def __init__(self, session: AsyncSession, settings: Settings, remnawave: RemnawaveGateway) -> None:
        self.session = session
        self.settings = settings
        self.notifications = NotificationService(session, settings, remnawave)
        self.accounts = AccountService(session, settings, remnawave)
        self.backups = BackupService(session, settings, remnawave)
        self.catalog = CatalogService(session, settings, remnawave)
        self.online = OnlineService(session, settings, remnawave)
        self.portal_auth = PortalAuthService(session, settings, remnawave)
        self.support = SupportService(session, settings, remnawave)
        self.promos = PromoService(session=session, settings=settings, remnawave=remnawave, accounts=self.accounts)
        self.billing = BillingService(
            session=session,
            settings=settings,
            remnawave=remnawave,
            accounts=self.accounts,
            catalog=self.catalog,
            notifications=self.notifications,
            promos=self.promos,
        )
        self.topups = TopupService(
            session=session,
            settings=settings,
            remnawave=remnawave,
            accounts=self.accounts,
            notifications=self.notifications,
            catalog=self.catalog,
        )
        self.dashboard = DashboardService(session, settings, remnawave)
        self.monitoring = MonitoringService(session, settings, remnawave)
        self.external_api = ExternalApiService(session, settings, remnawave)


class AppContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine: AsyncEngine = create_engine(settings)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.remnawave = RemnawaveClient(settings)
        self._prepared = False
        self._prepare_lock = asyncio.Lock()

    async def prepare(self) -> None:
        if self._prepared:
            return
        async with self._prepare_lock:
            if self._prepared:
                return
            await ensure_runtime_schema(self.engine)
            async with session_scope(self.session_factory) as session:
                for seed in DEFAULT_PLAN_SEEDS:
                    plan = await session.scalar(select(Plan).where(Plan.code == seed["code"]))
                    if plan is None:
                        session.add(Plan(**seed))
                        continue
                    for field, value in seed.items():
                        setattr(plan, field, value)
            self._prepared = True

    @asynccontextmanager
    async def session(self):
        await self.prepare()
        async with session_scope(self.session_factory) as session:
            yield session

    @asynccontextmanager
    async def hub(self):
        async with self.session() as session:
            yield ServiceHub(session, self.settings, self.remnawave)

    async def close(self) -> None:
        await self.remnawave.aclose()
        await self.engine.dispose()
