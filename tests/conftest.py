from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from altlink.application.services.accounts import AccountService
from altlink.application.services.billing import BillingService
from altlink.application.services.catalog import CatalogService
from altlink.application.services.notifications import NotificationService
from altlink.application.services.topups import TopupService
from altlink.db import session_scope
from altlink.domain.plans import DEFAULT_PLAN_SEEDS
from altlink.infrastructure.db.models import Base, Plan
from altlink.infrastructure.remnawave_schemas import (
    RemoteConnectionKeys,
    RemoteSubscriptionInfo,
    RemoteSubscriptionInfoUser,
    RemoteSubscriptionRequestRecord,
    RemoteUsageResponse,
    RemoteUser,
    RemoteUserTraffic,
)
from altlink.settings import Settings


class FakeRemnawave:
    def __init__(self) -> None:
        self.users: dict[str, RemoteUser] = {}

    async def list_nodes(self):
        return []

    async def list_users(self):
        return list(self.users.values())

    async def get_user(self, user_uuid: str):
        return self.users[user_uuid]

    async def find_user_by_telegram_id(self, telegram_id: int):
        for user in self.users.values():
            if user.telegramId == telegram_id:
                return user
        return None

    async def create_user(self, payload: dict):
        user_uuid = str(uuid4())
        short_uuid = payload.get("shortUuid") or f"sub-{user_uuid[:8]}"
        remote = self._build_user(
            user_uuid=user_uuid,
            username=payload["username"],
            telegram_id=payload.get("telegramId"),
            expire_at=datetime.fromisoformat(payload["expireAt"]),
            short_uuid=short_uuid,
            status=payload.get("status", "ACTIVE"),
            traffic_limit_bytes=int(payload.get("trafficLimitBytes", 0)),
        )
        self.users[user_uuid] = remote
        return remote

    async def update_user(self, payload: dict):
        remote = self.users.get(payload["uuid"])
        if remote is None:
            return await self.create_user(payload)
        updated = self._build_user(
            user_uuid=remote.uuid,
            username=payload.get("username", remote.username),
            telegram_id=payload.get("telegramId", remote.telegramId),
            expire_at=datetime.fromisoformat(payload["expireAt"]),
            short_uuid=remote.shortUuid,
            status=payload.get("status", remote.status),
            traffic_limit_bytes=int(payload.get("trafficLimitBytes", remote.trafficLimitBytes)),
            used_bytes=remote.userTraffic.usedTrafficBytes,
            lifetime_used_bytes=remote.userTraffic.lifetimeUsedTrafficBytes,
        )
        self.users[remote.uuid] = updated
        return updated

    async def enable_user(self, user_uuid: str):
        user = self.users[user_uuid]
        self.users[user_uuid] = self._build_user(
            user_uuid=user.uuid,
            username=user.username,
            telegram_id=user.telegramId,
            expire_at=user.expireAt,
            short_uuid=user.shortUuid,
            status="ACTIVE",
            traffic_limit_bytes=user.trafficLimitBytes,
            used_bytes=user.userTraffic.usedTrafficBytes,
            lifetime_used_bytes=user.userTraffic.lifetimeUsedTrafficBytes,
        )
        return self.users[user_uuid]

    async def disable_user(self, user_uuid: str):
        user = self.users[user_uuid]
        self.users[user_uuid] = self._build_user(
            user_uuid=user.uuid,
            username=user.username,
            telegram_id=user.telegramId,
            expire_at=user.expireAt,
            short_uuid=user.shortUuid,
            status="DISABLED",
            traffic_limit_bytes=user.trafficLimitBytes,
            used_bytes=user.userTraffic.usedTrafficBytes,
            lifetime_used_bytes=user.userTraffic.lifetimeUsedTrafficBytes,
        )
        return self.users[user_uuid]

    async def reset_user_traffic(self, user_uuid: str):
        user = self.users[user_uuid]
        self.users[user_uuid] = self._build_user(
            user_uuid=user.uuid,
            username=user.username,
            telegram_id=user.telegramId,
            expire_at=user.expireAt,
            short_uuid=user.shortUuid,
            status=user.status,
            traffic_limit_bytes=user.trafficLimitBytes,
            used_bytes=0,
            lifetime_used_bytes=user.userTraffic.lifetimeUsedTrafficBytes,
        )
        return self.users[user_uuid]

    async def delete_user(self, user_uuid: str):
        self.users.pop(user_uuid, None)
        return {}

    async def get_accessible_nodes(self, user_uuid: str):
        return []

    async def get_subscription_info(self, short_uuid: str):
        return RemoteSubscriptionInfo(
            isFound=True,
            user=RemoteSubscriptionInfoUser(
                shortUuid=short_uuid,
                daysLeft=30,
                trafficUsed="0",
                trafficLimit="0",
                lifetimeTrafficUsed="0",
                trafficUsedBytes="0",
                trafficLimitBytes="0",
                lifetimeTrafficUsedBytes="0",
                username="demo",
                expiresAt=datetime.now(UTC),
                isActive=True,
                userStatus="ACTIVE",
                trafficLimitStrategy="no_reset",
            ),
            links=[],
            ssConfLinks={},
            subscriptionUrl=f"https://sub.example/{short_uuid}",
        )

    async def get_connection_keys(self, user_uuid: str):
        return RemoteConnectionKeys(enabledKeys=[f"vmess://{user_uuid}"], hiddenKeys=[], disabledKeys=[])

    async def get_user_usage(self, user_uuid: str, start: date, end: date):
        return RemoteUsageResponse(categories=[], sparklineData=[], topNodes=[], series=[])

    async def get_subscription_request_history(self, user_uuid: str):
        return [
            RemoteSubscriptionRequestRecord(
                id=1,
                userUuid=user_uuid,
                requestIp="127.0.0.1",
                userAgent="Fake Client",
                requestAt=datetime.now(UTC),
            )
        ]

    async def healthcheck(self):
        return True

    async def aclose(self):
        return None

    def set_usage(self, user_uuid: str, used_bytes: int, lifetime_used_bytes: int | None = None) -> None:
        user = self.users[user_uuid]
        self.users[user_uuid] = self._build_user(
            user_uuid=user.uuid,
            username=user.username,
            telegram_id=user.telegramId,
            expire_at=user.expireAt,
            short_uuid=user.shortUuid,
            status=user.status,
            traffic_limit_bytes=user.trafficLimitBytes,
            used_bytes=used_bytes,
            lifetime_used_bytes=lifetime_used_bytes or used_bytes,
        )

    def _build_user(
        self,
        *,
        user_uuid: str,
        username: str,
        telegram_id: int | None,
        expire_at: datetime,
        short_uuid: str,
        status: str,
        traffic_limit_bytes: int,
        used_bytes: int = 0,
        lifetime_used_bytes: int = 0,
    ) -> RemoteUser:
        return RemoteUser(
            uuid=user_uuid,
            id=1,
            shortUuid=short_uuid,
            username=username,
            status=status,
            trafficLimitBytes=traffic_limit_bytes,
            trafficLimitStrategy="no_reset",
            expireAt=expire_at,
            telegramId=telegram_id,
            email=None,
            description=None,
            tag=None,
            hwidDeviceLimit=None,
            externalSquadUuid=None,
            trojanPassword="trojan-password",
            vlessUuid=str(uuid4()),
            ssPassword="ss-password",
            lastTriggeredThreshold=0,
            subRevokedAt=None,
            subLastUserAgent="Fake Client",
            subLastOpenedAt=None,
            lastTrafficResetAt=None,
            createdAt=datetime.now(UTC),
            updatedAt=datetime.now(UTC),
            subscriptionUrl=f"https://sub.example/{short_uuid}",
            activeInternalSquads=[],
            userTraffic=RemoteUserTraffic(
                usedTrafficBytes=used_bytes,
                lifetimeUsedTrafficBytes=lifetime_used_bytes,
                onlineAt=datetime.now(UTC),
                firstConnectedAt=datetime.now(UTC),
                lastConnectedNodeUuid=None,
            ),
        )


class TestServices:
    def __init__(self, session_factory, settings, remnawave):
        self.session_factory = session_factory
        self.settings = settings
        self.remnawave = remnawave

    @asynccontextmanager
    async def hub(self):
        async with session_scope(self.session_factory) as session:
            notifications = NotificationService(session, self.settings, self.remnawave)
            accounts = AccountService(session, self.settings, self.remnawave)
            catalog = CatalogService(session, self.settings, self.remnawave)
            billing = BillingService(
                session=session,
                settings=self.settings,
                remnawave=self.remnawave,
                accounts=accounts,
                catalog=catalog,
                notifications=notifications,
            )
            topups = TopupService(
                session=session,
                settings=self.settings,
                remnawave=self.remnawave,
                accounts=accounts,
                notifications=notifications,
            )
            yield SimpleNamespace(
                session=session,
                settings=self.settings,
                remnawave=self.remnawave,
                notifications=notifications,
                accounts=accounts,
                catalog=catalog,
                billing=billing,
                topups=topups,
            )


@pytest_asyncio.fixture
async def test_services(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'altlink-test.db'}"
    settings = Settings(
        _env_file=None,
        debug=False,
        database_url=database_url,
        session_secret_key="test-session",
        secret_key="test-secret",
        admin_api_key="test-api-key",
        client_bot_token="client-token",
        admin_bot_token="admin-token",
        remnawave_base_url="https://remna.example",
        remnawave_api_token="token",
    )
    engine = create_async_engine(settings.database_url, future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    remnawave = FakeRemnawave()

    async with session_scope(session_factory) as session:
        for seed in DEFAULT_PLAN_SEEDS:
            session.add(Plan(**seed))

    services = TestServices(session_factory, settings, remnawave)
    try:
        yield services
    finally:
        await engine.dispose()
