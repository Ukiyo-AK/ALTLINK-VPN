from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from altlink.application.services.accounts import AccountService
from altlink.application.services.base import BaseService, ConflictError, NotFoundError
from altlink.application.services.catalog import CatalogService
from altlink.application.services.notifications import NotificationService
from altlink.application.services.promos import PromoService
from altlink.domain.billing import bytes_to_gb_cost, compute_period_end, quantize_money
from altlink.domain.enums import (
    AccessStatus,
    BalanceTransactionType,
    NotificationType,
    PlanCode,
    ServerType,
    SubscriptionStatus,
    SystemEventLevel,
    UserStatus,
)
from altlink.domain.notifications import (
    blocked_message,
    low_balance_message,
    trial_ended_message,
    upcoming_renewal_message,
)
from altlink.domain.plans import WHITELIST_GB_PRICE_RUB, is_metered_plan_code
from altlink.infrastructure.db.models import Plan, Server, Subscription, TrafficSnapshot, TrialPeriod, User
from altlink.utils.time import ensure_utc, utc_now

LOW_BALANCE_REMINDER_WINDOWS: tuple[tuple[str, timedelta, timedelta, str], ...] = (
    ("3d", timedelta(days=3), timedelta(days=1), "меньше 3 дней"),
    ("1d", timedelta(days=1), timedelta(hours=1), "меньше 1 дня"),
    ("1h", timedelta(hours=1), timedelta(0), "меньше 1 часа"),
)


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
        promos: PromoService,
    ) -> None:
        super().__init__(session, settings, remnawave)
        self.accounts = accounts
        self.catalog = catalog
        self.notifications = notifications
        self.promos = promos

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
            cycle_days_processed=0,
            accrued_debt_rub=Decimal("0"),
            traffic_limit_bytes=plan.traffic_limit_bytes,
            traffic_used_bytes=0,
            whitelist_traffic_used_bytes=0,
            whitelist_traffic_billed_bytes=0,
            last_traffic_reset_at=now,
            auto_renew=False,
        )
        subscription.plan = plan
        self.session.add(subscription)

        if existing_trial is None:
            self.session.add(TrialPeriod(user_id=user.id, started_at=now, ends_at=ends_at, consumed=True))
        else:
            existing_trial.started_at = now
            existing_trial.ends_at = ends_at
            existing_trial.consumed = True

        await self.catalog.assign_preferred_server(user.id, plan.code)
        user.status = UserStatus.TRIAL
        await self.catalog.rebuild_user_access_matrix()
        await self._sync_remote_state(user, subscription, plan, enable=True, reset_traffic=True)
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

        existing = await self.accounts.get_current_subscription(user_id)
        preserve_traffic_on_switch = existing is not None
        discount_rub = Decimal("0.00")
        discount_promo = None
        discount_redemption = None
        if charge_user:
            discount_rub, discount_promo, discount_redemption = await self.promos.calculate_discount(
                user.id,
                Decimal(plan.price_rub),
            )
        discounted_price = quantize_money(Decimal(plan.price_rub) - discount_rub)
        carryover_credit_rub = self._calculate_residual_credit_rub(existing)

        if charge_user and carryover_credit_rub > 0:
            await self.accounts.adjust_balance(
                user_id=user.id,
                amount_rub=carryover_credit_rub,
                transaction_type=BalanceTransactionType.REFUND,
                description="Компенсация остатка прошлого тарифа при смене плана",
                admin_id=admin_id,
            )

        upfront_charge = discounted_price
        if charge_user and Decimal(user.balance_rub) < upfront_charge:
            raise ConflictError(f"Недостаточно средств. Для старта тарифа нужно минимум {upfront_charge:.2f} ₽.")

        if existing:
            existing.status = SubscriptionStatus.CANCELED
            existing.canceled_at = utc_now()

        if charge_user and upfront_charge > 0:
            await self.accounts.adjust_balance(
                user_id=user.id,
                amount_rub=-upfront_charge,
                transaction_type=BalanceTransactionType.SUBSCRIPTION_CHARGE,
                description=self._charge_description(plan, initial=True),
                admin_id=admin_id,
            )

        now = utc_now()
        ends_at = compute_period_end(now, plan.period_days)
        notes: list[str] = []
        if discount_promo is not None and discount_rub > 0:
            notes.append(f"Промокод {discount_promo.code}: скидка {discount_rub:.2f} ₽.")
        subscription = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            started_at=now,
            ends_at=ends_at,
            next_billing_at=ends_at,
            billing_anchor_at=now,
            cycle_days_processed=plan.period_days,
            accrued_debt_rub=Decimal("0"),
            traffic_limit_bytes=plan.traffic_limit_bytes,
            traffic_used_bytes=existing.traffic_used_bytes if existing else 0,
            whitelist_traffic_used_bytes=existing.whitelist_traffic_used_bytes if existing else 0,
            whitelist_traffic_billed_bytes=0,
            last_traffic_reset_at=existing.last_traffic_reset_at if existing else now,
            auto_renew=True,
            notes=" ".join(notes) or None,
        )
        subscription.plan = plan
        self.session.add(subscription)

        existing_trial = await self.session.scalar(select(TrialPeriod).where(TrialPeriod.user_id == user.id))
        if existing_trial:
            existing_trial.converted_to_subscription = True
        user.status = UserStatus.ACTIVE

        if is_metered_plan_code(plan.code):
            await self.catalog.assign_preferred_server(user.id, plan.code)

        await self.catalog.rebuild_user_access_matrix()
        remote_user = await self._sync_remote_state(
            user,
            subscription,
            plan,
            enable=True,
            reset_traffic=not preserve_traffic_on_switch,
        )
        if remote_user is not None and preserve_traffic_on_switch:
            await self._apply_remote_usage(user, subscription, remote_user)
        if discount_redemption is not None and discount_rub > 0:
            await self.promos.consume_discount(
                discount_redemption,
                subscription_id=subscription.id,
                applied_amount=discount_rub,
            )
        if charge_user:
            await self.accounts.grant_referral_bonus_if_eligible(user.id)
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="plan_activated",
            message="Активирован платный тариф.",
            payload={
                "user_id": user.id,
                "plan_code": plan.code.value,
                "upfront_charge": str(upfront_charge),
                "discount_rub": str(discount_rub),
                "carryover_credit_rub": str(carryover_credit_rub),
            },
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
        subscription.canceled_at = None
        user.status = UserStatus.ACTIVE
        plan = await self.accounts.get_plan(subscription.plan.code)
        await self.catalog.rebuild_user_access_matrix()
        await self._sync_remote_state(user, subscription, plan, enable=True, reset_traffic=False)
        return user

    async def cancel_subscription_renewal(self, user_id: str) -> Subscription:
        subscription = await self.accounts.get_current_subscription(user_id)
        if subscription is None or subscription.plan is None or subscription.plan.is_trial:
            raise ConflictError("Отключить автопродление можно только у платной подписки.")
        subscription.auto_renew = False
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="subscription_auto_renew_disabled",
            message="Автопродление подписки отключено пользователем.",
            payload={"user_id": user_id, "subscription_id": subscription.id},
        )
        return subscription

    async def restore_subscription_renewal(self, user_id: str) -> Subscription:
        subscription = await self.accounts.get_current_subscription(user_id)
        if subscription is None or subscription.plan is None or subscription.plan.is_trial:
            raise ConflictError("Включить автопродление можно только у платной подписки.")
        subscription.auto_renew = True
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="subscription_auto_renew_enabled",
            message="Автопродление подписки снова включено.",
            payload={"user_id": user_id, "subscription_id": subscription.id},
        )
        return subscription

    async def process_due_subscriptions(self) -> None:
        now = utc_now()
        subscriptions = list(
            (
                await self.session.scalars(
                    select(Subscription)
                    .options(
                        joinedload(Subscription.plan),
                        joinedload(Subscription.user).joinedload(User.assigned_server),
                        joinedload(Subscription.user).joinedload(User.trial_period),
                    )
                    .where(
                        Subscription.status.in_(
                            [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE]
                        )
                    )
                )
            ).all()
        )

        state_changed = False
        for subscription in subscriptions:
            user = subscription.user
            plan = subscription.plan
            if user is None or plan is None:
                continue

            if subscription.status == SubscriptionStatus.TRIAL and ensure_utc(subscription.ends_at) <= now:
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
                state_changed = True
                continue

            if subscription.status == SubscriptionStatus.GRACE:
                subscription.status = SubscriptionStatus.ACTIVE
                if user.status == UserStatus.GRACE:
                    user.status = UserStatus.ACTIVE

            if subscription.status != SubscriptionStatus.ACTIVE:
                continue

            if is_metered_plan_code(plan.code):
                await self._refresh_whitelist_usage(user, subscription)

            renewal_charge = self._compute_renewal_charge(subscription, plan)
            due_at = ensure_utc(subscription.next_billing_at)
            if due_at <= now:
                if not subscription.auto_renew:
                    await self._cancel_subscription(subscription, user)
                    state_changed = True
                    continue

                if Decimal(user.balance_rub) < renewal_charge:
                    await self._block_subscription(subscription, user)
                    state_changed = True
                    continue

                if renewal_charge > 0:
                    await self.accounts.adjust_balance(
                        user_id=user.id,
                        amount_rub=-renewal_charge,
                        transaction_type=BalanceTransactionType.SUBSCRIPTION_CHARGE,
                        description=self._charge_description(plan),
                    )
                await self._start_next_billing_cycle(subscription, plan)
                user.status = UserStatus.ACTIVE
                await self._sync_remote_state(user, subscription, plan, enable=True, reset_traffic=True)
                state_changed = True
                continue

            if due_at - now <= timedelta(days=1):
                await self.notifications.queue(
                    user_id=user.id,
                    notification_type=NotificationType.UPCOMING_RENEWAL,
                    message=upcoming_renewal_message(renewal_charge, due_at),
                    dedupe_key=f"renewal:{subscription.id}:{now.date().isoformat()}",
                )
            threshold = Decimal(self.settings.low_balance_threshold_rub)
            reminder_window = self._low_balance_reminder_window(due_at - now)
            if Decimal(user.balance_rub) <= max(threshold, renewal_charge) and reminder_window is not None:
                reminder_key, reminder_label = reminder_window
                await self.notifications.queue(
                    user_id=user.id,
                    notification_type=NotificationType.LOW_BALANCE,
                    message=low_balance_message(
                        Decimal(user.balance_rub),
                        renewal_charge,
                        due_at,
                        reminder_label,
                    ),
                    dedupe_key=f"low-balance:{subscription.id}:{due_at.isoformat()}:{reminder_key}",
                )

        if state_changed:
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
            if subscription is None:
                continue

            await self._apply_remote_usage(user, subscription, remote_user)

        await self.catalog.rebuild_user_access_matrix()

    async def refresh_subscription_traffic(self, user_id: str) -> Subscription | None:
        subscription = await self.accounts.get_current_subscription(user_id)
        if subscription is None:
            return None

        user = await self.accounts.get_user(user_id)
        if self.remnawave is None or not user.remnawave_user_uuid:
            return subscription

        try:
            remote_user = await self.remnawave.get_user(user.remnawave_user_uuid)
        except httpx.HTTPError:
            return subscription

        self.session.add(
            TrafficSnapshot(
                user_id=user.id,
                subscription_id=subscription.id,
                server_id=None,
                snapshot_date=date.today(),
                used_bytes=remote_user.userTraffic.usedTrafficBytes,
                lifetime_used_bytes=remote_user.userTraffic.lifetimeUsedTrafficBytes,
                source="remnawave_refresh",
            )
        )
        await self._apply_remote_usage(user, subscription, remote_user)
        return subscription

    async def _cancel_subscription(self, subscription: Subscription, user: User) -> None:
        subscription.status = SubscriptionStatus.CANCELED
        subscription.canceled_at = utc_now()
        user.status = UserStatus.CANCELED
        if self.remnawave and user.remnawave_user_uuid:
            await self.remnawave.disable_user(user.remnawave_user_uuid)

    async def _block_subscription(self, subscription: Subscription, user: User) -> None:
        subscription.status = SubscriptionStatus.BLOCKED
        subscription.blocked_at = utc_now()
        user.status = UserStatus.BLOCKED
        if self.remnawave and user.remnawave_user_uuid:
            await self.remnawave.disable_user(user.remnawave_user_uuid)
        await self.notifications.queue(
            user_id=user.id,
            notification_type=NotificationType.ACCESS_BLOCKED,
            message=blocked_message(),
            dedupe_key=f"blocked:{subscription.id}",
        )

    async def _start_next_billing_cycle(self, subscription: Subscription, plan: Plan) -> None:
        subscription.started_at = ensure_utc(subscription.ends_at)
        subscription.ends_at = compute_period_end(subscription.started_at, plan.period_days)
        subscription.next_billing_at = subscription.ends_at
        subscription.billing_anchor_at = subscription.started_at
        subscription.cycle_days_processed = plan.period_days
        subscription.accrued_debt_rub = Decimal("0")
        subscription.grace_started_at = None
        subscription.grace_until = None
        subscription.traffic_used_bytes = 0
        subscription.whitelist_traffic_used_bytes = 0
        subscription.whitelist_traffic_billed_bytes = 0
        subscription.last_traffic_reset_at = utc_now()
        if self.remnawave and subscription.user and subscription.user.remnawave_user_uuid:
            await self.remnawave.reset_user_traffic(subscription.user.remnawave_user_uuid)

    async def _refresh_whitelist_usage(self, user: User, subscription: Subscription) -> None:
        if self.remnawave is None or not user.remnawave_user_uuid:
            return
        whitelist_node_ids = {
            server.remnawave_node_uuid
            for server in (
                await self.session.scalars(select(Server).where(Server.server_type == ServerType.WHITELIST))
            ).all()
        }
        if not whitelist_node_ids:
            subscription.whitelist_traffic_used_bytes = 0
            return
        usage = await self.remnawave.get_user_usage(
            user.remnawave_user_uuid,
            ensure_utc(subscription.started_at).date(),
            date.today(),
        )
        total = sum(node.total for node in usage.topNodes if node.uuid in whitelist_node_ids)
        subscription.whitelist_traffic_used_bytes = max(total, 0)

    async def _apply_remote_usage(self, user: User, subscription: Subscription, remote_user) -> None:
        user.last_seen_at = remote_user.userTraffic.onlineAt or user.last_seen_at
        subscription.traffic_used_bytes = remote_user.userTraffic.usedTrafficBytes
        if remote_user.lastTrafficResetAt is not None:
            subscription.last_traffic_reset_at = remote_user.lastTrafficResetAt
        if subscription.plan and is_metered_plan_code(subscription.plan.code):
            await self._refresh_whitelist_usage(user, subscription)

    def _compute_renewal_charge(self, subscription: Subscription, plan: Plan) -> Decimal:
        whitelist_charge = Decimal("0.00")
        if is_metered_plan_code(plan.code):
            whitelist_charge = bytes_to_gb_cost(subscription.whitelist_traffic_used_bytes, WHITELIST_GB_PRICE_RUB)
        return quantize_money(Decimal(plan.price_rub) + whitelist_charge)

    def _charge_description(self, plan: Plan, *, initial: bool = False) -> str:
        period_label = "еженедельное" if plan.period_days <= 7 else "ежемесячное"
        prefix = "Первое" if initial else "Продление"
        return f"{prefix} {period_label} списание по тарифу {plan.name}"

    @staticmethod
    def _low_balance_reminder_window(remaining: timedelta) -> tuple[str, str] | None:
        if remaining <= timedelta(0):
            return None
        for reminder_key, upper_bound, lower_bound, reminder_label in LOW_BALANCE_REMINDER_WINDOWS:
            if lower_bound < remaining <= upper_bound:
                return reminder_key, reminder_label
        return None

    def _calculate_residual_credit_rub(self, existing: Subscription | None) -> Decimal:
        if existing is None or existing.plan is None or existing.plan.is_trial:
            return Decimal("0.00")
        remaining = ensure_utc(existing.ends_at) - utc_now()
        if remaining.total_seconds() <= 0 or existing.plan.period_days <= 0:
            return Decimal("0.00")
        current_price = Decimal(existing.plan.price_rub)
        if current_price <= 0:
            return Decimal("0.00")
        unused_share = Decimal(str(remaining.total_seconds())) / Decimal(existing.plan.period_days * 86400)
        return max(quantize_money(current_price * unused_share), Decimal("0.00"))

    async def _sync_remote_state(
        self,
        user: User,
        subscription: Subscription,
        plan: Plan,
        *,
        enable: bool,
        reset_traffic: bool,
    ):
        if self.remnawave is None:
            return None

        await self.accounts.ensure_remote_user_link(user)
        user_servers = await self.catalog.get_user_servers(user.id)
        squad_ids = [
            access.server.remnawave_internal_squad_uuid
            for access in user_servers
            if access.status in {AccessStatus.ACTIVE, AccessStatus.GRACE}
            and access.server
            and access.server.remnawave_internal_squad_uuid
        ]
        expire_at = subscription.grace_until if subscription.status == SubscriptionStatus.GRACE else subscription.ends_at
        payload = {
            "uuid": user.remnawave_user_uuid,
            "username": user.remnawave_username or f"tg_{user.telegram_id}",
            "status": "ACTIVE" if enable else "DISABLED",
            "expireAt": expire_at.isoformat(),
            "trafficLimitBytes": int(subscription.traffic_limit_bytes or 0),
            "trafficLimitStrategy": "NO_RESET",
            "telegramId": user.telegram_id,
            "description": f"ALTLINK user {user.telegram_id}",
            "activeInternalSquads": squad_ids,
            "hwidDeviceLimit": plan.device_limit,
        }
        try:
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
                try:
                    await self.remnawave.enable_user(remote.uuid)
                except httpx.HTTPStatusError as exc:
                    if not self._is_ignorable_state_error(exc, "enabled"):
                        raise
            else:
                try:
                    await self.remnawave.disable_user(remote.uuid)
                except httpx.HTTPStatusError as exc:
                    if not self._is_ignorable_state_error(exc, "disabled"):
                        raise
            if reset_traffic:
                await self.remnawave.reset_user_traffic(remote.uuid)
                remote = await self.remnawave.get_user(remote.uuid)
            return remote
        except httpx.HTTPStatusError as exc:
            raise ConflictError(self._format_remnawave_error(exc)) from exc

    def _is_ignorable_state_error(self, exc: httpx.HTTPStatusError, desired_state: str) -> bool:
        response = exc.response
        try:
            payload = response.json()
        except ValueError:
            payload = None

        messages: list[str] = []
        if isinstance(payload, dict):
            message = payload.get("message")
            if message:
                messages.append(str(message))
            details = payload.get("errors")
            if isinstance(details, list):
                for item in details:
                    if isinstance(item, dict):
                        detail_message = item.get("message")
                        if detail_message:
                            messages.append(str(detail_message))

        haystack = " ".join(messages).lower()
        return f"already {desired_state}" in haystack

    def _format_remnawave_error(self, exc: httpx.HTTPStatusError) -> str:
        response = exc.response
        try:
            payload = response.json()
        except ValueError:
            payload = None

        errors = []
        if isinstance(payload, dict):
            details = payload.get("errors")
            if isinstance(details, list):
                for item in details:
                    if isinstance(item, dict):
                        message = item.get("message") or item.get("code")
                        path = item.get("path")
                        if isinstance(path, list) and path:
                            message = f"{'.'.join(str(part) for part in path)}: {message}"
                        if message:
                            errors.append(str(message))
            message = payload.get("message")
            if message and not errors:
                errors.append(str(message))

        detail = "; ".join(errors) if errors else response.text[:300]
        return f"Remnawave отклонил операцию: {detail}"
