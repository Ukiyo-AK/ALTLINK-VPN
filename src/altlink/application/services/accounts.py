from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import desc, select

from altlink.application.services.base import ServiceBase
from altlink.domain.enums import EventLevel
from altlink.infrastructure.db.models import Plan, Subscription, TrialPeriod, User


class AccountService(ServiceBase):
    async def register_or_update_telegram_user(
        self,
        *,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        language_code: str | None,
    ) -> User:
        user = await self.get_user_by_telegram_id(telegram_id)
        now = datetime.now(UTC)
        if user is None:
            user = User(
                telegram_id=telegram_id,
                telegram_username=username,
                first_name=first_name,
                last_name=last_name,
                language_code=language_code,
                last_seen_at=now,
                last_bot_interaction_at=now,
            )
            self.session.add(user)
            await self.session.flush()
            await self.log_event(
                scope="users",
                level=EventLevel.INFO,
                title="Новый пользователь зарегистрирован",
                user_id=user.id,
            )
        else:
            user.telegram_username = username
            user.first_name = first_name
            user.last_name = last_name
            user.language_code = language_code
            user.last_seen_at = now
            user.last_bot_interaction_at = now
        return user

    async def get_profile_summary(self, user: User) -> dict:
        subscription = await self.get_current_subscription(user.id)
        trial = (
            await self.session.execute(
                select(TrialPeriod)
                .where(TrialPeriod.user_id == user.id)
                .order_by(desc(TrialPeriod.created_at))
            )
        ).scalar_one_or_none()
        plan = await self.session.get(Plan, subscription.plan_id) if subscription else None
        return {
            "user": user,
            "subscription": subscription,
            "plan": plan,
            "trial": trial,
        }

    async def list_subscription_history(self, user: User):
        result = await self.session.execute(
            select(Plan, Subscription)
            .join(Subscription, Subscription.plan_id == Plan.id)
            .where(Subscription.user_id == user.id)
            .order_by(desc(Subscription.updated_at))
        )
        return result.all()
