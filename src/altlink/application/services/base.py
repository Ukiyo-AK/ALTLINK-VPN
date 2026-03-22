from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from altlink.domain.enums import EventLevel, SubscriptionStatus
from altlink.infrastructure.db.models import Subscription, SystemEvent, User
from altlink.infrastructure.remnawave import RemnawaveClient
from altlink.settings import Settings


class ServiceBase:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        remnawave: RemnawaveClient | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.remnawave = remnawave
        self.logger = logging.getLogger(self.__class__.__name__)

    async def log_event(
        self,
        *,
        scope: str,
        level: EventLevel,
        title: str,
        details: str | None = None,
        payload: dict[str, Any] | None = None,
        user_id: str | None = None,
        server_id: str | None = None,
        subscription_id: str | None = None,
    ) -> SystemEvent:
        event = SystemEvent(
            scope=scope,
            level=level,
            title=title,
            details=details,
            payload=payload,
            user_id=user_id,
            server_id=server_id,
            subscription_id=subscription_id,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        result = await self.session.execute(select(User).where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()

    async def get_current_subscription(self, user_id: str) -> Subscription | None:
        result = await self.session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.is_current.is_(True),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def decimal_to_str(value: Decimal) -> str:
        return f"{value:.2f}"

    @staticmethod
    def is_subscription_serviceable(status: SubscriptionStatus) -> bool:
        return status in {SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE}

