from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from altlink.application.services.base import ServiceBase
from altlink.domain.constants import TRIAL_PLAN_CODE
from altlink.domain.enums import (
    BalanceTransactionType,
    EventLevel,
    PlanKind,
    SubscriptionStatus,
    TrialStatus,
    UserStatus,
)
from altlink.infrastructure.db.models import BalanceTransaction, Plan, Subscription, TrialPeriod, User
from altlink.infrastructure.remnawave import RemnawaveNotFoundError


class SubscriptionService(ServiceBase):
    async def ensure_plan(self, code: str) -> Plan:
        result = await self.session.execute(select(Plan).where(Plan.code == code, Plan.is_active.is_(True)))
        plan = result.scalar_one_or_none()
        if plan is None:
            raise ValueError("Тариф не найден")
        return plan

    async def ensure_remnawave_user(
        self,
        user: User,
        *,
        expire_at: datetime,
        traffic_limit_bytes: int | None,
        description: str | None,
    ):
        if self.remnawave is None:
            raise RuntimeError("Remnawave client is required")

        remote_user = None
        if user.remnawave_user_uuid:
            try:
                remote_user = await self.remnawave.get_user_by_uuid(user.remnawave_user_uuid)
            except RemnawaveNotFoundError:
                remote_user = None
        if remote_user is None:
            existing = await self.remnawave.get_users_by_telegram_id(user.telegram_id)
            if existing:
                remote_user = existing[0]
                remote_user = await self.remnawave.update_user(
                    uuid=remote_user.uuid,
                    expire_at=expire_at,
                    traffic_limit_bytes=traffic_limit_bytes or 0,
                    description=description,
                    telegram_id=user.telegram_id,
                )
            else:
                remote_user = await self.remnawave.create_user(
                    username=user.remnawave_username or f"tg_{user.telegram_id}",
                    expire_at=expire_at,
                    traffic_limit_bytes=traffic_limit_bytes,
                    telegram_id=user.telegram_id,
                    description=description,
                )
        else:
            remote_user = await self.remnawave.update_user(
                uuid=remote_user.uuid,
                expire_at=expire_at,
                traffic_limit_bytes=traffic_limit_bytes or 0,
                description=description,
                telegram_id=user.telegram_id,
            )

        user.remnawave_user_uuid = remote_user.uuid
        user.remnawave_username = remote_user.username
        user.remnawave_short_uuid = remote_user.shortUuid
        user.remnawave_subscription_url = remote_user.subscriptionUrl
        user.last_remnawave_sync_at = datetime.now(UTC)
        return remote_user

    async def activate_trial(self, user: User, *, admin_user_id: str | None = None) -> Subscription:
        if user.status != UserStatus.NEW:
            raise ValueError("Тестовый период можно получить только для нового пользователя.")
        existing_trial = await self.session.execute(select(TrialPeriod).where(TrialPeriod.user_id == user.id))
        if existing_trial.scalar_one_or_none() is not None:
            raise ValueError("Тестовый период уже был использован.")

        plan = await self.ensure_plan(TRIAL_PLAN_CODE)
        now = datetime.now(UTC)
        trial_end = now + timedelta(days=self.settings.trial_duration_days)

        subscription = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=SubscriptionStatus.TRIAL,
            is_current=True,
            is_trial=True,
            started_at=now,
            first_activated_at=now,
            current_period_start=now,
            current_period_end=trial_end,
            next_billing_at=trial_end,
            renewal_price_rub=Decimal("0.00"),
            traffic_limit_bytes_snapshot=None,
        )
        self.session.add(subscription)
        await self.session.flush()

        trial = TrialPeriod(
            user_id=user.id,
            subscription_id=subscription.id,
            activated_by_admin_id=admin_user_id,
            status=TrialStatus.ACTIVE,
            started_at=now,
            ends_at=trial_end,
        )
        self.session.add(trial)

        user.status = UserStatus.TRIAL
        await self.ensure_remnawave_user(
            user,
            expire_at=trial_end,
            traffic_limit_bytes=None,
            description="ALTLINK trial",
        )
        if user.remnawave_user_uuid and self.remnawave:
            await self.remnawave.enable_user(user.remnawave_user_uuid)
        await self.log_event(
            scope="subscriptions",
            level=EventLevel.INFO,
            title="Тестовый период активирован",
            user_id=user.id,
            subscription_id=subscription.id,
        )
        return subscription

    async def activate_paid_plan(
        self,
        user: User,
        plan_code: str,
        *,
        charge_immediately: bool = True,
        comment: str | None = None,
    ) -> Subscription:
        plan = await self.ensure_plan(plan_code)
        if plan.kind == PlanKind.TRIAL:
            raise ValueError("Тестовый тариф нельзя активировать как платный.")
        now = datetime.now(UTC)
        current = await self.get_current_subscription(user.id)
        if current is None:
            subscription = Subscription(user_id=user.id, plan_id=plan.id, is_current=True)
            self.session.add(subscription)
            await self.session.flush()
        else:
            subscription = current

        if charge_immediately and user.balance_rub < plan.price_rub:
            raise ValueError("Недостаточно средств на балансе.")

        if charge_immediately:
            before = user.balance_rub
            user.balance_rub -= plan.price_rub
            self.session.add(
                BalanceTransaction(
                    user_id=user.id,
                    subscription_id=subscription.id,
                    transaction_type=BalanceTransactionType.RENEWAL,
                    amount_rub=-plan.price_rub,
                    balance_before=before,
                    balance_after=user.balance_rub,
                    comment=comment or f"Активация тарифа {plan.name_ru}",
                )
            )

        subscription.plan_id = plan.id
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.is_trial = False
        subscription.started_at = subscription.started_at or now
        subscription.first_activated_at = subscription.first_activated_at or now
        subscription.current_period_start = now
        subscription.current_period_end = now + timedelta(days=plan.duration_days)
        subscription.next_billing_at = subscription.current_period_end
        subscription.grace_started_at = None
        subscription.grace_ends_at = None
        subscription.blocked_at = None
        subscription.debt_rub = Decimal("0.00")
        subscription.renewal_price_rub = plan.price_rub
        subscription.traffic_limit_bytes_snapshot = plan.traffic_limit_bytes
        subscription.traffic_used_bytes_cache = 0
        subscription.last_traffic_reset_at = now
        subscription.notes = comment

        user.status = UserStatus.ACTIVE

        remote_expire_at = subscription.current_period_end + timedelta(days=self.settings.grace_period_days)
        await self.ensure_remnawave_user(
            user,
            expire_at=remote_expire_at,
            traffic_limit_bytes=plan.traffic_limit_bytes,
            description=f"ALTLINK {plan.name_ru}",
        )
        if user.remnawave_user_uuid and self.remnawave:
            await self.remnawave.enable_user(user.remnawave_user_uuid)
            if plan.traffic_limit_bytes:
                await self.remnawave.reset_user_traffic(user.remnawave_user_uuid)

        await self.log_event(
            scope="subscriptions",
            level=EventLevel.INFO,
            title="Платный тариф активирован",
            user_id=user.id,
            subscription_id=subscription.id,
            details=plan.code,
        )
        return subscription

    async def block_user_access(self, user: User, subscription: Subscription, *, reason: str) -> None:
        subscription.status = SubscriptionStatus.BLOCKED
        subscription.blocked_at = datetime.now(UTC)
        user.status = UserStatus.BLOCKED
        if user.remnawave_user_uuid and self.remnawave:
            await self.remnawave.disable_user(user.remnawave_user_uuid)
        await self.log_event(
            scope="subscriptions",
            level=EventLevel.WARNING,
            title="Доступ пользователя заблокирован",
            details=reason,
            user_id=user.id,
            subscription_id=subscription.id,
        )

    async def manual_set_active(self, user: User, plan_code: str) -> Subscription:
        return await self.activate_paid_plan(user, plan_code, charge_immediately=False, comment="Активация админом")

    async def manual_deactivate(self, user: User) -> None:
        subscription = await self.get_current_subscription(user.id)
        if subscription is None:
            raise ValueError("У пользователя нет активной подписки")
        await self.block_user_access(user, subscription, reason="Ручная деактивация администратором")
