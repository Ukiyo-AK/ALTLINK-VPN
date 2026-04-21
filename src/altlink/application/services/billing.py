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
from altlink.domain.billing import (
    bytes_to_gb_cost,
    compute_period_end,
    compute_prorated_daily_charge,
    evaluate_renewal,
    quantize_money,
)
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
    grace_reminder_message,
    grace_started_message,
    low_balance_message,
    trial_ended_message,
    upcoming_renewal_message,
)
from altlink.domain.plans import WHITELIST_GB_PRICE_RUB
from altlink.infrastructure.db.models import Plan, Server, Subscription, TrafficSnapshot, TrialPeriod, User
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
            cycle_days_processed=0,
            accrued_debt_rub=Decimal("0"),
            traffic_limit_bytes=plan.traffic_limit_bytes,
            traffic_used_bytes=0,
            whitelist_traffic_used_bytes=0,
            whitelist_traffic_billed_bytes=0,
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

        await self.catalog.assign_preferred_server(user.id)
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

        first_day_charge = compute_prorated_daily_charge(Decimal(plan.price_rub), plan.period_days, 1)
        if charge_user and Decimal(user.balance_rub) < first_day_charge:
            raise ConflictError(
                f"Недостаточно средств. Для старта тарифа нужно минимум {first_day_charge:.2f} ₽."
            )

        existing = await self.accounts.get_current_subscription(user_id)
        if existing:
            existing.status = SubscriptionStatus.CANCELED
            existing.canceled_at = utc_now()

        if charge_user and first_day_charge > 0:
            await self.accounts.adjust_balance(
                user_id=user.id,
                amount_rub=-first_day_charge,
                transaction_type=BalanceTransactionType.SUBSCRIPTION_CHARGE,
                description=f"Первое суточное списание по тарифу {plan.name}",
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
            next_billing_at=now + timedelta(days=1),
            billing_anchor_at=now,
            cycle_days_processed=1,
            accrued_debt_rub=Decimal("0"),
            traffic_limit_bytes=plan.traffic_limit_bytes,
            traffic_used_bytes=0,
            whitelist_traffic_used_bytes=0,
            whitelist_traffic_billed_bytes=0,
            last_traffic_reset_at=now,
        )
        subscription.plan = plan
        self.session.add(subscription)
        existing_trial = await self.session.scalar(select(TrialPeriod).where(TrialPeriod.user_id == user.id))
        if existing_trial:
            existing_trial.converted_to_subscription = True
        user.status = UserStatus.ACTIVE

        if plan.code == PlanCode.SINGLE_10GBIT:
            await self.catalog.assign_preferred_server(user.id)

        await self.catalog.rebuild_user_access_matrix()
        await self._sync_remote_state(user, subscription, plan, enable=True, reset_traffic=True)
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="plan_activated",
            message="Активирован платный тариф.",
            payload={
                "user_id": user.id,
                "plan_code": plan.code.value,
                "first_day_charge": str(first_day_charge),
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
        subscription.grace_started_at = None
        subscription.grace_until = None
        user.status = UserStatus.ACTIVE
        plan = await self.accounts.get_plan(subscription.plan.code)
        await self.catalog.rebuild_user_access_matrix()
        await self._sync_remote_state(user, subscription, plan, enable=True, reset_traffic=False)
        return user

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

            if subscription.status == SubscriptionStatus.GRACE and subscription.grace_until and now > ensure_utc(
                subscription.grace_until
            ):
                await self._block_subscription(subscription, user)
                state_changed = True
                continue

            while subscription.status in {SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE} and ensure_utc(
                subscription.next_billing_at
            ) <= now:
                await self._roll_billing_cycle_if_needed(subscription, plan)
                if plan.code == PlanCode.SINGLE_10GBIT:
                    await self._refresh_whitelist_usage(user, subscription)

                next_day_number = subscription.cycle_days_processed + 1
                base_daily_charge = compute_prorated_daily_charge(
                    Decimal(plan.price_rub),
                    plan.period_days,
                    next_day_number,
                )
                whitelist_charge = self._compute_whitelist_daily_charge(subscription, plan)
                decision = evaluate_renewal(
                    balance_rub=Decimal(user.balance_rub),
                    charge_rub=base_daily_charge + whitelist_charge,
                    debt_rub=Decimal(subscription.accrued_debt_rub),
                    now=now,
                    grace_started_at=subscription.grace_started_at,
                    grace_days=self.settings.grace_period_days,
                )

                if decision.action == "charge":
                    total_charge = quantize_money(
                        Decimal(subscription.accrued_debt_rub) + base_daily_charge + whitelist_charge
                    )
                    if total_charge > 0:
                        await self.accounts.adjust_balance(
                            user_id=user.id,
                            amount_rub=-total_charge,
                            transaction_type=BalanceTransactionType.SUBSCRIPTION_CHARGE,
                            description=f"Суточное списание по тарифу {plan.name}",
                        )
                    subscription.accrued_debt_rub = Decimal("0")
                    subscription.status = SubscriptionStatus.ACTIVE
                    subscription.grace_started_at = None
                    subscription.grace_until = None
                    subscription.cycle_days_processed += 1
                    subscription.whitelist_traffic_billed_bytes = subscription.whitelist_traffic_used_bytes
                    subscription.next_billing_at = ensure_utc(subscription.next_billing_at) + timedelta(days=1)
                    user.status = UserStatus.ACTIVE
                    await self._sync_remote_state(user, subscription, plan, enable=True, reset_traffic=False)
                    state_changed = True
                    continue

                daily_due = quantize_money(base_daily_charge + whitelist_charge)
                subscription.accrued_debt_rub = quantize_money(Decimal(subscription.accrued_debt_rub) + daily_due)
                subscription.cycle_days_processed += 1
                subscription.whitelist_traffic_billed_bytes = subscription.whitelist_traffic_used_bytes
                subscription.next_billing_at = ensure_utc(subscription.next_billing_at) + timedelta(days=1)

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
                            Decimal(subscription.accrued_debt_rub),
                            decision.grace_until,
                        ),
                        dedupe_key=f"grace-started:{subscription.id}",
                    )
                    await self._sync_remote_state(user, subscription, plan, enable=True, reset_traffic=False)
                    state_changed = True
                    continue

                if decision.action == "keep_grace":
                    subscription.status = SubscriptionStatus.GRACE
                    subscription.grace_until = decision.grace_until
                    user.status = UserStatus.GRACE
                    await self.notifications.queue(
                        user_id=user.id,
                        notification_type=NotificationType.GRACE_REMINDER,
                        message=grace_reminder_message(
                            Decimal(subscription.accrued_debt_rub),
                            decision.grace_until,
                        ),
                        dedupe_key=f"grace-reminder:{subscription.id}:{now.date().isoformat()}",
                    )
                    state_changed = True
                    if decision.grace_until and now > decision.grace_until:
                        await self._block_subscription(subscription, user)
                    continue

                await self._block_subscription(subscription, user)
                state_changed = True
                break

            if subscription.status == SubscriptionStatus.ACTIVE:
                next_charge = compute_prorated_daily_charge(
                    Decimal(plan.price_rub),
                    plan.period_days,
                    min(subscription.cycle_days_processed + 1, plan.period_days),
                )
                if ensure_utc(subscription.next_billing_at) - now <= timedelta(days=1):
                    await self.notifications.queue(
                        user_id=user.id,
                        notification_type=NotificationType.UPCOMING_RENEWAL,
                        message=upcoming_renewal_message(next_charge, ensure_utc(subscription.next_billing_at)),
                        dedupe_key=f"renewal:{subscription.id}:{now.date().isoformat()}",
                    )
                threshold = Decimal(self.settings.low_balance_threshold_rub)
                if Decimal(user.balance_rub) <= max(threshold, next_charge):
                    await self.notifications.queue(
                        user_id=user.id,
                        notification_type=NotificationType.LOW_BALANCE,
                        message=low_balance_message(
                            Decimal(user.balance_rub),
                            next_charge,
                            ensure_utc(subscription.next_billing_at),
                        ),
                        dedupe_key=f"low-balance:{subscription.id}:{now.date().isoformat()}",
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
            user.last_seen_at = remote_user.userTraffic.onlineAt or user.last_seen_at
            if subscription is None:
                continue

            subscription.traffic_used_bytes = remote_user.userTraffic.usedTrafficBytes
            if subscription.plan and subscription.plan.code == PlanCode.SINGLE_10GBIT:
                await self._refresh_whitelist_usage(user, subscription)

        await self.catalog.rebuild_user_access_matrix()

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

    async def _roll_billing_cycle_if_needed(self, subscription: Subscription, plan: Plan) -> None:
        if subscription.cycle_days_processed < plan.period_days:
            return
        subscription.started_at = ensure_utc(subscription.ends_at)
        subscription.ends_at = compute_period_end(subscription.started_at, plan.period_days)
        subscription.cycle_days_processed = 0
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

    def _compute_whitelist_daily_charge(self, subscription: Subscription, plan: Plan) -> Decimal:
        if plan.code != PlanCode.SINGLE_10GBIT:
            return Decimal("0.00")
        delta_bytes = max(subscription.whitelist_traffic_used_bytes - subscription.whitelist_traffic_billed_bytes, 0)
        return bytes_to_gb_cost(delta_bytes, WHITELIST_GB_PRICE_RUB)

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
