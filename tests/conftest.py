from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from altlink.domain.constants import DEFAULT_PLANS
from altlink.domain.enums import PlanKind
from altlink.infrastructure.db.base import Base
from altlink.infrastructure.db.models import Plan


class FakeRemnawave:
    def __init__(self) -> None:
        self.created_users = []
        self.enabled = []
        self.disabled = []
        self.updated = []
        self.reset = []
        self.users = {}

    async def get_user_by_uuid(self, uuid: str):
        return self.users[uuid]

    async def get_users_by_telegram_id(self, telegram_id: int):
        return [user for user in self.users.values() if user.telegramId == telegram_id]

    async def create_user(self, **kwargs):
        user = SimpleNamespace(
            uuid=f"remote-{len(self.users) + 1}",
            username=kwargs["username"],
            shortUuid=f"short-{len(self.users) + 1}",
            subscriptionUrl=f"https://sub.example/{len(self.users) + 1}",
            telegramId=kwargs["telegram_id"],
        )
        self.users[user.uuid] = user
        self.created_users.append(kwargs)
        return user

    async def update_user(self, **kwargs):
        uuid = kwargs["uuid"]
        self.updated.append(kwargs)
        user = self.users.get(uuid)
        if user is None:
            user = SimpleNamespace(
                uuid=uuid,
                username=f"updated-{uuid}",
                shortUuid=f"short-{uuid}",
                subscriptionUrl=f"https://sub.example/{uuid}",
                telegramId=kwargs.get("telegram_id"),
            )
            self.users[uuid] = user
        return user

    async def enable_user(self, uuid: str):
        self.enabled.append(uuid)
        return self.users.get(uuid)

    async def disable_user(self, uuid: str):
        self.disabled.append(uuid)
        return self.users.get(uuid)

    async def reset_user_traffic(self, uuid: str):
        self.reset.append(uuid)
        return self.users.get(uuid)


@pytest.fixture
def test_settings():
    return SimpleNamespace(
        trial_duration_days=2,
        grace_period_days=14,
        traffic_notify_thresholds=[70, 90, 100],
        low_balance_threshold_rub=50,
        low_balance_notify_days=5,
        client_bot_token="test-token",
    )


@pytest.fixture
def fake_remnawave():
    return FakeRemnawave()


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        for item in DEFAULT_PLANS:
            session.add(
                Plan(
                    code=item["code"],
                    name_ru=item["name_ru"],
                    kind=PlanKind(item["kind"]),
                    price_rub=item["price_rub"],
                    duration_days=item["duration_days"],
                    traffic_limit_bytes=item["traffic_limit_bytes"],
                    sort_order=item["sort_order"],
                    is_trial=item["is_trial"],
                    is_active=True,
                )
            )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()
