from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from altlink.application.services.accounts import AccountService
from altlink.application.services.base import BaseService, ConflictError, NotFoundError
from altlink.application.services.catalog import CatalogService
from altlink.application.services.notifications import NotificationService
from altlink.domain.billing import compute_period_end, detect_threshold_crossing, evaluate_renewal
from altlink.domain.enums import (
    BalanceTransactionType,
    NotificationType,
    PlanCode,
    SubscriptionStatus,
    SystemEventLevel,
    UserStatus,
)
from altlink.domain.notifications import (
    blocked_message,
    grace_reminder_message,
    grace_started_message,
    low_balance_message,
    traffic_exceeded_message,
    traffic_threshold_message,
    trial_ended_message,
    upcoming_renewal_message,
)
from altlink.infrastructure.db.models import Plan, Subscription, TrafficSnapshot, TrialPeriod, User
from altlink.utils.time import ensure_utc, utc_now


class BillingService(BaseService):
    source = "billing"

    def __init__(
        self,
        *,
        session,
        settings,
        remnawave,
        accounts: AccountService,
        catalog: CatalogService,
        notifications: NotificationService,
    ) -> None:
        super().__init__(session, settings, remnawave)
        self.accounts = accounts
        self.catalog = catalog
        self.notifications = notifications

    async def activate_trial(self, user_id: str) -> Subscription:
        user = await self.accounts.get_user(user_id)
        current = await self.accounts.get_current_subscription(user_id)
        if current and current.status in {
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.TRIAL,
            SubscriptionStatus.GRACE,
        }:
            raise ConflictError("У пользователя уже есть активный доступ.")

        existing_trial = await self.session.scalar(select(TrialPeriod).where(TrialPeriod.user_id == user.id))
        if existing_trial and existing_trial.consumed:
            raise ConflictError("Тестовый период уже был использован.")

        plan = await self.accounts.get_plan(PlanCode.TRIAL)
        now = utc_now()
        ends_at = compute_period_end(now, self.settings.trial_duration_days or plan.period_days)
        subscription = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=SubscriptionStatus.TRIAL,
            started_at=now,
            ends_at=ends_at,
            next_billing_at=ends_at,
            billing_anchor_at=now,
            traffic_limit_bytes=plan.traffic_limit_bytes,
            traffic_used_bytes=0,
            last_traffic_reset_at=now,
        )
        subscription.plan = plan
        self.session.add(subscription)

        if existing_trial is None:
            self.session.add(TrialPeriod(user_id=user.id, started_at=now, ends_at=ends_at, consumed=True))
        else:
            existing_trial.started_at = now
            existing_trial.ends_at = ends_at
            existing_trial.consumed = True

        user.status = UserStatus.TRIAL
        await self._sync_remote_state(user, subscription, plan, enable=True, reset_traffic=True)
        await self.catalog.rebuild_user_access_matrix()
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="trial_activated",
            message="Активирован тестовый период.",
            payload={"user_id": user.id},
        )
        return subscription

    async def activate_paid_plan(
        self,
        user_id: str,
        plan_code: PlanCode,
        *,
        charge_user: bool = True,
        admin_id: str | None = None,
    ) -> Subscription:
        user = await self.accounts.get_user(user_id)
        plan = await self.accounts.get_plan(plan_code)
        if plan.is_trial:
            raise ConflictError("Для тестового периода используйте отдельный сценарий.")
        if charge_user and Decimal(user.balance_rub) < Decimal(plan.price_rub):
            raise ConflictError("Недостаточно средств на балансе.")

        existing = await self.accounts.get_current_subscription(user_id)
        if existing:
            existing.status = SubscriptionStatus.CANCELED
            existing.canceled_at = utc_now()

        if charge_user and Decimal(plan.price_rub) > 0:
            await self.accounts.adjust_balance(
                user_id=user.id,
                amount_rub=-Decimal(plan.price_rub),
                transaction_type=BalanceTransactionType.SUBSCRIPTION_CHARGE,
                description=f"Активация тарифа {plan.name}",
                admin_id=admin_id,
            )

        now = utc_now()
        ends_at = compute_period_end(now, plan.period_days)
        subscription = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            started_at=now,
            ends_at=ends_at,
            next_billing_at=ends_at,
            billing_anchor_at=now,
            traffic_limit_bytes=plan.traffic_limit_bytes,
            traffic_used_bytes=0,
            last_traffic_reset_at=now,
        )
        subscription.plan = plan
        self.session.add(subscription)
        existing_trial = await self.session.scalar(select(TrialPeriod).where(TrialPeriod.user_id == user.id))
        if existing_trial:
            existing_trial.converted_to_subscription = True
        user.status = UserStatus.ACTIVE

        await self._sync_remote_state(user, subscription, plan, enable=True, reset_traffic=True)
        await self.catalog.rebuild_user_access_matrix()
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="plan_activated",
            message="Активирован платный тариф.",
            payload={"user_id": user.id, "plan_code": plan.code.value},
            actor_admin_id=admin_id,
        )
        return subscription

    async def deactivate_user(self, user_id: str) -> User:
        user = await self.accounts.get_user(user_id)
        subscription = await self.accounts.get_current_subscription(user.id)
        if subscription:
            subscription.status = SubscriptionStatus.BLOCKED
            subscription.blocked_at = utc_now()
        user.status = UserStatus.BLOCKED
        if self.remnawave and user.remnawave_user_uuid:
            await self.remnawave.disable_user(user.remnawave_user_uuid)
        await self.catalog.rebuild_user_access_matrix()
        return user

    async def reactivate_user(self, user_id: str) -> User:
        user = await self.accounts.get_user(user_id)
        subscription = await self.accounts.get_latest_subscription(user.id)
        if subscription is None:
            raise NotFoundError("У пользователя нет подписки для повторной активации.")
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.blocked_at = None
        user.status = UserStatus.ACTIVE
        plan = await self.accounts.get_plan(subscription.plan.code)
        await self._sync_remote_state(user, subscription, plan, enable=True, reset_traffic=False)
        await self.catalog.rebuild_user_access_matrix()
        return user

    async def process_due_subscriptions(self) -> None:
        now = utc_now()
        subscriptions = list(
            (
                await self.session.scalars(
                    select(Subscription)
                    .options(
                        joinedload(Subscription.plan),
                        joinedload(Subscription.user).joinedload(User.trial_period),
                    )
                    .where(
                        Subscription.status.in_(
                            [
                                SubscriptionStatus.ACTIVE,
                                SubscriptionStatus.GRACE,
                                SubscriptionStatus.TRIAL,
                            ]
                        )
                    )
                )
            ).all()
        )

        for subscription in subscriptions:
            user = subscription.user
            plan = subscription.plan
            if user is None or plan is None:
                continue
            next_billing_at = ensure_utc(subscription.next_billing_at)
            ends_at = ensure_utc(subscription.ends_at)
            grace_until = ensure_utc(subscription.grace_until) if subscription.grace_until else None

            if subscription.status == SubscriptionStatus.TRIAL and ends_at <= now:
                subscription.status = SubscriptionStatus.EXPIRED
                user.status = UserStatus.BLOCKED
                if self.remnawave and user.remnawave_user_uuid:
                    await self.remnawave.disable_user(user.remnawave_user_uuid)
                await self.notifications.queue(
                    user_id=user.id,
                    notification_type=NotificationType.TRIAL_ENDED,
                    message=trial_ended_message(),
                    dedupe_key=f"trial-ended:{subscription.id}",
                )
                await self.notifications.queue(
                    user_id=user.id,
                    notification_type=NotificationType.ACCESS_BLOCKED,
                    message=blocked_message(),
                    dedupe_key=f"trial-blocked:{subscription.id}",
                )
                continue

            if subscription.status == SubscriptionStatus.ACTIVE and next_billing_at <= now:
                decision = evaluate_renewal(
                    balance_rub=Decimal(user.balance_rub),
                    price_rub=Decimal(plan.price_rub),
                    now=now,
                    grace_started_at=subscription.grace_started_at,
                    grace_days=self.settings.grace_period_days,
                )
                if decision.action == "charge":
                    await self.accounts.adjust_balance(
                        user_id=user.id,
                        amount_rub=-Decimal(plan.price_rub),
                        transaction_type=BalanceTransactionType.SUBSCRIPTION_CHARGE,
                        description=f"Плановое продление тарифа {plan.name}",
                    )
                    new_start = next_billing_at
                    subscription.status = SubscriptionStatus.ACTIVE
                    subscription.started_at = new_start
                    subscription.ends_at = compute_period_end(new_start, plan.period_days)
                    subscription.next_billing_at = subscription.ends_at
                    subscription.grace_started_at = None
                    subscription.grace_until = None
                    subscription.traffic_used_bytes = 0
                    subscription.last_notification_threshold = 0
                    subscription.last_traffic_reset_at = now
                    user.status = UserStatus.ACTIVE
                    await self._sync_remote_state(
                        user, subscription, plan, enable=True, reset_traffic=plan.traffic_limit_bytes is not None
                    )
                    continue

                if decision.action == "enter_grace":
                    subscription.status = SubscriptionStatus.GRACE
                    subscription.grace_started_at = now
                    subscription.grace_until = decision.grace_until
                    user.status = UserStatus.GRACE
                    await self.notifications.queue(
                        user_id=user.id,
                        notification_type=NotificationType.GRACE_STARTED,
                        message=grace_started_message(
                            Decimal(user.balance_rub),
                            Decimal(plan.price_rub) - Decimal(user.balance_rub),
                            decision.grace_until,
                        ),
                        dedupe_key=f"grace-started:{subscription.id}",
                    )
                    await self._sync_remote_state(user, subscription, plan, enable=True, reset_traffic=False)
                    continue

            if subscription.status == SubscriptionStatus.GRACE:
                if (
                    Decimal(user.balance_rub) >= Decimal(plan.price_rub)
                    and grace_until
                    and grace_until >= now
                ):
                    await self.accounts.adjust_balance(
                        user_id=user.id,
                        amount_rub=-Decimal(plan.price_rub),
                        transaction_type=BalanceTransactionType.SUBSCRIPTION_CHARGE,
                        description=f"Выход из grace period и продление тарифа {plan.name}",
                    )
                    new_start = next_billing_at
                    subscription.status = SubscriptionStatus.ACTIVE
                    subscription.started_at = new_start
                    subscription.ends_at = compute_period_end(new_start, plan.period_days)
                    subscription.next_billing_at = subscription.ends_at
                    subscription.grace_started_at = None
                    subscription.grace_until = None
                    subscription.traffic_used_bytes = 0
                    subscription.last_notification_threshold = 0
                    user.status = UserStatus.ACTIVE
                    await self._sync_remote_state(
                        user, subscription, plan, enable=True, reset_traffic=plan.traffic_limit_bytes is not None
                    )
                    continue

                if grace_until and now > grace_until:
                    subscription.status = SubscriptionStatus.BLOCKED
                    subscription.blocked_at = now
                    user.status = UserStatus.BLOCKED
                    if self.remnawave and user.remnawave_user_uuid:
                        await self.remnawave.disable_user(user.remnawave_user_uuid)
                    await self.notifications.queue(
                        user_id=user.id,
                        notification_type=NotificationType.ACCESS_BLOCKED,
                        message=blocked_message(),
                        dedupe_key=f"blocked:{subscription.id}",
                    )
                elif grace_until:
                    await self.notifications.queue(
                        user_id=user.id,
                        notification_type=NotificationType.GRACE_REMINDER,
                        message=grace_reminder_message(
                            Decimal(plan.price_rub) - Decimal(user.balance_rub),
                            grace_until,
                        ),
                        dedupe_key=f"grace-reminder:{subscription.id}:{now.date().isoformat()}",
                    )

            days_to_renewal = (next_billing_at - now).days
            if subscription.status == SubscriptionStatus.ACTIVE and 0 <= days_to_renewal <= 3:
                await self.notifications.queue(
                    user_id=user.id,
                    notification_type=NotificationType.UPCOMING_RENEWAL,
                    message=upcoming_renewal_message(Decimal(plan.price_rub), next_billing_at),
                    dedupe_key=f"renewal:{subscription.id}:{now.date().isoformat()}",
                )

            if (
                subscription.status == SubscriptionStatus.ACTIVE
                and Decimal(user.balance_rub) < Decimal(plan.price_rub)
                and 0 <= days_to_renewal <= 3
            ):
                await self.notifications.queue(
                    user_id=user.id,
                    notification_type=NotificationType.LOW_BALANCE,
                    message=low_balance_message(
                        Decimal(user.balance_rub), Decimal(plan.price_rub), next_billing_at
                    ),
                    dedupe_key=f"low-balance:{subscription.id}:{now.date().isoformat()}",
                )

        await self.catalog.rebuild_user_access_matrix()

    async def snapshot_traffic(self) -> None:
        if self.remnawave is None:
            return

        now = utc_now()
        users = list((await self.session.scalars(select(User))).all())
        user_map = {user.remnawave_user_uuid: user for user in users if user.remnawave_user_uuid}
        remote_users = await self.remnawave.list_users()

        for remote_user in remote_users:
            user = user_map.get(remote_user.uuid)
            if user is None:
                continue

            subscription = await self.accounts.get_current_subscription(user.id)
            self.session.add(
                TrafficSnapshot(
                    user_id=user.id,
                    subscription_id=subscription.id if subscription else None,
                    server_id=None,
                    snapshot_date=date.today(),
                    used_bytes=remote_user.userTraffic.usedTrafficBytes,
                    lifetime_used_bytes=remote_user.userTraffic.lifetimeUsedTrafficBytes,
                    source="remnawave",
                )
            )
            user.last_seen_at = remote_user.userTraffic.onlineAt or user.last_seen_at
            if subscription is None:
                continue

            subscription.traffic_used_bytes = remote_user.userTraffic.usedTrafficBytes
            if subscription.traffic_limit_bytes:
                threshold = detect_threshold_crossing(
                    used_bytes=subscription.traffic_used_bytes,
                    limit_bytes=subscription.traffic_limit_bytes,
                    thresholds=self.settings.traffic_notification_thresholds,
                    last_threshold=subscription.last_notification_threshold,
                )
                if threshold:
                    subscription.last_notification_threshold = threshold
                    limit_gb = subscription.traffic_limit_bytes / 1024**3
                    used_gb = subscription.traffic_used_bytes / 1024**3
                    if threshold >= 100 or subscription.traffic_used_bytes >= subscription.traffic_limit_bytes:
                        subscription.status = SubscriptionStatus.BLOCKED
                        subscription.blocked_at = now
                        user.status = UserStatus.BLOCKED
                        if self.remnawave and user.remnawave_user_uuid:
                            await self.remnawave.disable_user(user.remnawave_user_uuid)
                        await self.notifications.queue(
                            user_id=user.id,
                            notification_type=NotificationType.TRAFFIC_EXCEEDED,
                            message=traffic_exceeded_message(limit_gb),
                            dedupe_key=f"traffic-exceeded:{subscription.id}",
                        )
                        await self.notifications.queue(
                            user_id=user.id,
                            notification_type=NotificationType.ACCESS_BLOCKED,
                            message=blocked_message(),
                            dedupe_key=f"traffic-blocked:{subscription.id}",
                        )
                    else:
                        await self.notifications.queue(
                            user_id=user.id,
                            notification_type=NotificationType.TRAFFIC_THRESHOLD,
                            message=traffic_threshold_message(threshold, used_gb, limit_gb),
                            dedupe_key=f"traffic-threshold:{subscription.id}:{threshold}",
                        )

        await self.catalog.rebuild_user_access_matrix()

    async def _sync_remote_state(
        self,
        user: User,
        subscription: Subscription,
        plan: Plan,
        *,
        enable: bool,
        reset_traffic: bool,
    ) -> None:
        if self.remnawave is None:
            return

        await self.accounts.ensure_remote_user_link(user)
        expire_at = subscription.grace_until if subscription.status == SubscriptionStatus.GRACE else subscription.ends_at
        payload = {
            "uuid": user.remnawave_user_uuid,
            "username": user.remnawave_username or f"tg_{user.telegram_id}",
            "status": "ACTIVE" if enable else "DISABLED",
            "expireAt": expire_at.isoformat(),
            "trafficLimitBytes": int(subscription.traffic_limit_bytes or 0),
            "trafficLimitStrategy": "no_reset",
            "telegramId": user.telegram_id,
            "description": f"ALTLINK user {user.telegram_id}",
        }
        if user.remnawave_user_uuid:
            remote = await self.remnawave.update_user(payload)
        else:
            payload.pop("uuid", None)
            remote = await self.remnawave.create_user(payload)

        user.remnawave_user_uuid = remote.uuid
        user.remnawave_username = remote.username
        user.remnawave_short_uuid = remote.shortUuid
        subscription.remnawave_synced_at = utc_now()
        if enable:
            await self.remnawave.enable_user(remote.uuid)
        else:
            await self.remnawave.disable_user(remote.uuid)
        if reset_traffic:
            await self.remnawave.reset_user_traffic(remote.uuid)
