from __future__ import annotations

import logging
import hashlib
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import or_, select
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
    NotificationStatus,
    NotificationType,
    PlanCode,
    ServerType,
    SubscriptionStatus,
    SystemEventLevel,
    UserStatus,
)
from altlink.domain.notifications import (
    blocked_message,
    DISCOUNT_PROMO_TEMPLATE_IDS,
    inactive_subscription_promo_message,
    low_balance_message,
    renewal_disabled_expiring_message,
    RETURN_TRIAL_TEMPLATE_IDS,
    return_trial_offer_message,
    trial_followup_message,
    trial_expiring_message,
    trial_ended_message,
    trial_setup_help_message,
)
from altlink.domain.plans import WHITELIST_GB_PRICE_RUB, is_metered_plan_code
from altlink.infrastructure.db.models import (
    OnlineSessionCache,
    Notification,
    Plan,
    Server,
    Subscription,
    SystemSetting,
    TrafficSnapshot,
    TrialPeriod,
    User,
)
from altlink.utils.time import ensure_utc, utc_now

LOW_BALANCE_REMINDER_WINDOWS: tuple[tuple[str, timedelta, timedelta, str], ...] = (
    ("3d", timedelta(days=3), timedelta(days=1), "меньше 3 дней"),
    ("1d", timedelta(days=1), timedelta(hours=1), "меньше 1 дня"),
    ("1h", timedelta(hours=1), timedelta(0), "меньше 1 часа"),
)
START_WHITELIST_BALANCE_FLOOR_RUB = Decimal("-50.00")
TRIAL_REMINDER_WINDOWS: tuple[tuple[str, timedelta, timedelta, str], ...] = (
    ("24h", timedelta(days=1), timedelta(hours=3), "24 часа"),
    ("3h", timedelta(hours=3), timedelta(hours=1), "3 часа"),
    ("1h", timedelta(hours=1), timedelta(0), "1 час"),
)
TRIAL_SETUP_HELP_DELAY = timedelta(hours=3)
PROMO_CAMPAIGN_SETTINGS_KEY = "promo.campaign_settings"
DEFAULT_PROMO_CAMPAIGN_SETTINGS = {
    "new_user_discount_percent": 10,
    "lapsed_user_discount_percent": 35,
    "inactive_first_delay_days": 3,
    "lapsed_first_delay_days": 1,
    "deep_winback_delay_days": 30,
    "return_trial_enabled": True,
    "return_trial_cooldown_days": 30,
}

logger = logging.getLogger(__name__)
SyncProgressCallback = Callable[[dict[str, object]], Awaitable[None]]


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
        now = utc_now()
        current = await self.accounts.get_current_subscription(user_id)
        if current and current.status in {
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.TRIAL,
            SubscriptionStatus.GRACE,
        }:
            raise ConflictError("У пользователя уже есть активный доступ.")

        existing_trial = await self.session.scalar(select(TrialPeriod).where(TrialPeriod.user_id == user.id))
        if existing_trial and existing_trial.consumed:
            promo_settings = await self._promo_campaign_settings()
            cooldown_days = max(int(promo_settings["return_trial_cooldown_days"]), 0)
            repeat_allowed_at = ensure_utc(existing_trial.ends_at) + timedelta(days=cooldown_days)
            if not promo_settings["return_trial_enabled"] or now < repeat_allowed_at:
                raise ConflictError("Тестовый период уже был использован.")

        plan = await self.accounts.get_plan(PlanCode.TRIAL)
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
        await self._sync_user_remote_access(user, subscription, plan, enable=True, reset_traffic=True)
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
        upfront_charge = discounted_price
        effective_available_balance_rub = quantize_money(
            Decimal(user.balance_rub) + (carryover_credit_rub if charge_user else Decimal("0.00"))
        )
        if charge_user and effective_available_balance_rub < upfront_charge:
            raise ConflictError(f"Недостаточно средств. Для старта тарифа нужно минимум {upfront_charge:.2f} ₽.")

        if existing:
            existing.status = SubscriptionStatus.CANCELED
            existing.canceled_at = utc_now()

        if charge_user and carryover_credit_rub > 0:
            await self.accounts.adjust_balance(
                user_id=user.id,
                amount_rub=carryover_credit_rub,
                transaction_type=BalanceTransactionType.REFUND,
                description="Компенсация остатка прошлого тарифа при смене плана",
                admin_id=admin_id,
            )

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
            whitelist_traffic_billed_bytes=existing.whitelist_traffic_billed_bytes if existing else 0,
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
        remote_user = await self._sync_user_remote_access(
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
        await self._sync_user_remote_access(user, subscription, plan, enable=True, reset_traffic=False)
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

    async def sync_users_with_available_nodes(
        self,
        progress_callback: SyncProgressCallback | None = None,
    ) -> dict[str, object]:
        if self.remnawave is None:
            raise ConflictError("Remnawave panel is not configured.")

        summary: dict[str, object] = {
            "total": 0,
            "synced": 0,
            "created": 0,
            "updated": 0,
            "recreated": 0,
            "empty_squads": 0,
            "skipped": 0,
            "failed": 0,
            "catalog_synced": True,
            "errors": [],
        }

        async def notify_progress(stage: str, *, processed: int = 0, total: int | None = None) -> None:
            if progress_callback is None:
                return
            payload = {
                **summary,
                "stage": stage,
                "processed": processed,
                "total": int(summary["total"]) if total is None else total,
            }
            try:
                await progress_callback(payload)
            except Exception:  # noqa: BLE001
                logger.debug("User node access sync progress callback failed.", exc_info=True)

        await notify_progress("catalog")
        try:
            await self.catalog.sync_servers()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to sync server catalog before user node access sync.", exc_info=True)
            await self.session.rollback()
            summary["catalog_synced"] = False
            errors = summary["errors"]
            assert isinstance(errors, list)
            errors.append(f"catalog: {exc}")

        subscriptions = list(
            (
                await self.session.scalars(
                    select(Subscription)
                    .options(joinedload(Subscription.user), joinedload(Subscription.plan))
                    .where(
                        Subscription.status.in_(
                            [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE]
                        )
                    )
                    .order_by(Subscription.created_at.desc())
                )
            ).all()
        )

        unique_subscriptions: list[Subscription] = []
        seen_user_ids: set[str] = set()
        for subscription in subscriptions:
            if subscription.user_id in seen_user_ids:
                continue
            seen_user_ids.add(subscription.user_id)
            unique_subscriptions.append(subscription)

        summary["total"] = len(unique_subscriptions)
        await notify_progress("users_loaded", total=len(unique_subscriptions))

        active_user_statuses = {UserStatus.ACTIVE, UserStatus.TRIAL, UserStatus.GRACE}
        errors = summary["errors"]
        assert isinstance(errors, list)
        now = utc_now()
        for processed, subscription in enumerate(unique_subscriptions, start=1):
            user = subscription.user
            plan = subscription.plan
            if user is None or plan is None or user.status not in active_user_statuses:
                summary["skipped"] = int(summary["skipped"]) + 1
                await notify_progress("user_processed", processed=processed)
                continue
            expire_at = subscription.grace_until if subscription.status == SubscriptionStatus.GRACE else subscription.ends_at
            if ensure_utc(expire_at) <= now:
                summary["skipped"] = int(summary["skipped"]) + 1
                logger.warning(
                    "Skipping Remnawave node access sync for expired subscription %s of user %s.",
                    subscription.id,
                    user.id,
                )
                await notify_progress("user_processed", processed=processed)
                continue

            before_uuid = user.remnawave_user_uuid
            try:
                remote = await self._sync_user_remote_access(
                    user,
                    subscription,
                    plan,
                    enable=True,
                    reset_traffic=False,
                )
                await self.session.flush()
            except Exception as exc:  # noqa: BLE001
                summary["failed"] = int(summary["failed"]) + 1
                label = user.username or user.telegram_id
                errors.append(f"{label}: {exc}")
                logger.warning("Failed to sync available node squads for user %s.", user.id, exc_info=True)
                await notify_progress("user_processed", processed=processed)
                continue

            if remote is None:
                summary["skipped"] = int(summary["skipped"]) + 1
                await notify_progress("user_processed", processed=processed)
                continue

            summary["synced"] = int(summary["synced"]) + 1
            if not remote.activeInternalSquads:
                summary["empty_squads"] = int(summary["empty_squads"]) + 1
            if before_uuid is None:
                summary["created"] = int(summary["created"]) + 1
            elif before_uuid != user.remnawave_user_uuid:
                summary["recreated"] = int(summary["recreated"]) + 1
            else:
                summary["updated"] = int(summary["updated"]) + 1
            await notify_progress("user_processed", processed=processed)

        await self.log_event(
            level=SystemEventLevel.INFO if not int(summary["failed"]) else SystemEventLevel.WARNING,
            event_type="remnawave_user_node_access_synced",
            message="Manual Remnawave user node access sync completed.",
            payload={key: value for key, value in summary.items() if key != "errors"},
        )
        await notify_progress("completed", processed=int(summary["total"]))
        return summary

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
            state_changed = await self._process_due_subscription(subscription, now) or state_changed
            await self._commit_checkpoint()

        await self._queue_inactive_user_promos(now)
        await self._queue_post_trial_followups(now)
        await self._commit_checkpoint()

        if state_changed:
            await self.catalog.rebuild_user_access_matrix()
            await self._commit_checkpoint()

    async def _process_due_subscription(self, subscription: Subscription, now: datetime) -> bool:
        user = subscription.user
        plan = subscription.plan
        if user is None or plan is None:
            return False

        if subscription.status == SubscriptionStatus.TRIAL:
            return await self._sync_trial_subscription_state(subscription, now)

        was_grace = subscription.status == SubscriptionStatus.GRACE
        if was_grace:
            subscription.status = SubscriptionStatus.ACTIVE
            if user.status == UserStatus.GRACE:
                user.status = UserStatus.ACTIVE

        if subscription.status != SubscriptionStatus.ACTIVE:
            return False

        if is_metered_plan_code(plan.code):
            await self._refresh_whitelist_usage(user, subscription)
            await self._apply_instant_whitelist_charges(user, subscription)

        discount_rub, discount_promo, discount_redemption = await self.promos.calculate_discount(
            user.id,
            Decimal(plan.price_rub),
        )
        renewal_charge = self._compute_renewal_charge(
            subscription,
            plan,
            plan_discount_rub=discount_rub,
        )
        due_at = ensure_utc(subscription.next_billing_at)
        if due_at <= now:
            if not subscription.auto_renew:
                await self._cancel_subscription(subscription, user)
                return True

            if Decimal(user.balance_rub) < renewal_charge:
                await self._block_subscription(subscription, user, grace_ended=was_grace)
                return True

            if renewal_charge > 0:
                description = self._charge_description(plan)
                if discount_promo is not None and discount_rub > 0:
                    description += f" с промокодом {discount_promo.code}"
                await self.accounts.adjust_balance(
                    user_id=user.id,
                    amount_rub=-renewal_charge,
                    transaction_type=BalanceTransactionType.SUBSCRIPTION_CHARGE,
                    description=description,
                )
            await self._start_next_billing_cycle(subscription, plan, renewed_at=now)
            user.status = UserStatus.ACTIVE
            remote_user = await self._sync_user_remote_access(
                user,
                subscription,
                plan,
                enable=True,
                reset_traffic=True,
            )
            self._ensure_remote_expiration_synced(remote_user, subscription.ends_at)
            if discount_redemption is not None and discount_rub > 0:
                await self.promos.consume_discount(
                    discount_redemption,
                    subscription_id=subscription.id,
                    applied_amount=discount_rub,
                )
                discount_note = f"Автопродление с промокодом {discount_promo.code}: скидка {discount_rub:.2f} ₽."
                subscription.notes = " ".join(filter(None, [subscription.notes, discount_note]))
            await self.log_event(
                level=SystemEventLevel.INFO,
                event_type="subscription_auto_renewed",
                message="Подписка автоматически продлена и синхронизирована с Remnawave.",
                payload={
                    "user_id": user.id,
                    "subscription_id": subscription.id,
                    "plan_code": plan.code.value,
                    "charged_rub": str(renewal_charge),
                    "discount_rub": str(discount_rub),
                    "promo_code": discount_promo.code if discount_promo is not None else None,
                    "ends_at": subscription.ends_at.isoformat(),
                },
            )
            return True

        balance_rub = Decimal(user.balance_rub)
        if not subscription.auto_renew and due_at - now <= timedelta(days=1):
            await self.notifications.queue(
                user_id=user.id,
                notification_type=NotificationType.BROADCAST,
                message=renewal_disabled_expiring_message(
                    balance_rub,
                    renewal_charge,
                    due_at,
                ),
                payload={
                    "kind": "renewal_disabled_expiring",
                    "cta": "renewal_disabled_expiring",
                },
                dedupe_key=f"renewal-disabled-expiring:{subscription.id}:{due_at.isoformat()}",
            )
        needs_topup_for_renewal = subscription.auto_renew and balance_rub < renewal_charge
        threshold = Decimal(self.settings.low_balance_threshold_rub)
        reminder_window = self._low_balance_reminder_window(due_at - now)
        if (
            needs_topup_for_renewal
            and balance_rub <= max(threshold, renewal_charge)
            and reminder_window is not None
        ):
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
                payload={
                    "kind": "low_balance",
                    "cta": "low_balance",
                },
                dedupe_key=f"low-balance:{subscription.id}:{due_at.isoformat()}:{reminder_key}",
            )
        return False

    async def _commit_checkpoint(self) -> None:
        await self.session.commit()

    async def sync_user_trial_state(self, user_id: str) -> Subscription | None:
        subscription = await self.session.scalar(
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status.in_(
                    [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE]
                ),
            )
            .options(
                joinedload(Subscription.plan),
                joinedload(Subscription.user).joinedload(User.assigned_server),
                joinedload(Subscription.user).joinedload(User.trial_period),
            )
            .order_by(Subscription.created_at.desc())
        )
        if subscription is None or subscription.status != SubscriptionStatus.TRIAL:
            return subscription

        if await self._sync_trial_subscription_state(subscription, utc_now()):
            await self.catalog.rebuild_user_access_matrix()

        return await self.accounts.get_current_subscription(user_id)

    async def snapshot_traffic(self) -> None:
        if self.remnawave is None:
            return

        users = list((await self.session.scalars(select(User))).all())
        user_map = {user.remnawave_user_uuid: user for user in users if user.remnawave_user_uuid}
        remote_users = await self.remnawave.list_users()
        remote_pairs: list[tuple[User, Subscription | None, object]] = []

        for remote_user in remote_users:
            user = user_map.get(remote_user.uuid)
            if user is None:
                continue
            subscription = await self.accounts.get_current_subscription(user.id)
            remote_pairs.append((user, subscription, remote_user))

        whitelist_usage_by_remote_uuid = await self._load_whitelist_usage_by_user(
            {
                user.remnawave_user_uuid: ensure_utc(subscription.started_at).date()
                for user, subscription, _ in remote_pairs
                if (
                    user.remnawave_user_uuid
                    and subscription is not None
                    and subscription.plan is not None
                    and is_metered_plan_code(subscription.plan.code)
                )
            }
        )

        for user, subscription, remote_user in remote_pairs:
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

            await self._apply_remote_usage(
                user,
                subscription,
                remote_user,
                whitelist_usage_by_remote_uuid=whitelist_usage_by_remote_uuid,
            )

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
        await self._disable_remote_user_best_effort(user, event_type="subscription_cancel_disable_failed")

    async def _block_subscription(self, subscription: Subscription, user: User, *, grace_ended: bool = False) -> None:
        subscription.status = SubscriptionStatus.BLOCKED
        subscription.blocked_at = utc_now()
        user.status = UserStatus.BLOCKED
        await self._disable_remote_user_best_effort(user, event_type="subscription_block_disable_failed")
        await self.notifications.queue(
            user_id=user.id,
            notification_type=NotificationType.ACCESS_BLOCKED,
            message=blocked_message(grace_ended=grace_ended),
            payload={
                "kind": "access_blocked",
                "cta": "access_blocked",
            },
            dedupe_key=f"blocked:{subscription.id}",
        )

    async def _start_next_billing_cycle(
        self,
        subscription: Subscription,
        plan: Plan,
        *,
        renewed_at: datetime | None = None,
    ) -> None:
        current_time = ensure_utc(renewed_at or utc_now())
        subscription.started_at = max(ensure_utc(subscription.ends_at), current_time)
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

    async def _refresh_whitelist_usage(
        self,
        user: User,
        subscription: Subscription,
        *,
        preloaded_usage_by_remote_uuid: dict[str, int] | None = None,
    ) -> None:
        if self.remnawave is None or not user.remnawave_user_uuid:
            return
        if preloaded_usage_by_remote_uuid is not None and user.remnawave_user_uuid in preloaded_usage_by_remote_uuid:
            subscription.whitelist_traffic_used_bytes = max(preloaded_usage_by_remote_uuid[user.remnawave_user_uuid], 0)
            return

        whitelist_node_ids = await self._get_whitelist_node_ids()
        if not whitelist_node_ids:
            subscription.whitelist_traffic_used_bytes = 0
            return
        totals_by_user = await self._load_whitelist_usage_by_user(
            {user.remnawave_user_uuid: ensure_utc(subscription.started_at).date()}
        )
        if user.remnawave_user_uuid in totals_by_user:
            subscription.whitelist_traffic_used_bytes = max(totals_by_user[user.remnawave_user_uuid], 0)
            return
        usage = await self.remnawave.get_user_usage(
            user.remnawave_user_uuid,
            ensure_utc(subscription.started_at).date(),
            date.today(),
        )
        total = self._sum_whitelist_usage_from_user_stats(usage, whitelist_node_ids)
        subscription.whitelist_traffic_used_bytes = max(total, 0)

    async def _apply_remote_usage(
        self,
        user: User,
        subscription: Subscription,
        remote_user,
        *,
        whitelist_usage_by_remote_uuid: dict[str, int] | None = None,
    ) -> None:
        user.last_seen_at = remote_user.userTraffic.onlineAt or user.last_seen_at
        subscription.traffic_used_bytes = remote_user.userTraffic.usedTrafficBytes
        if remote_user.lastTrafficResetAt is not None:
            subscription.last_traffic_reset_at = remote_user.lastTrafficResetAt
        if subscription.plan and is_metered_plan_code(subscription.plan.code):
            await self._refresh_whitelist_usage(
                user,
                subscription,
                preloaded_usage_by_remote_uuid=whitelist_usage_by_remote_uuid,
            )
            await self._apply_instant_whitelist_charges(user, subscription)

    async def _get_whitelist_node_ids(self) -> set[str]:
        return {
            server.remnawave_node_uuid
            for server in (
                await self.session.scalars(select(Server).where(Server.server_type == ServerType.WHITELIST))
            ).all()
            if server.remnawave_node_uuid
        }

    async def _load_whitelist_usage_by_user(self, start_dates_by_remote_uuid: dict[str, date]) -> dict[str, int]:
        if self.remnawave is None or not start_dates_by_remote_uuid:
            return {}

        whitelist_node_ids = await self._get_whitelist_node_ids()
        if not whitelist_node_ids:
            return {}

        window_start = datetime.combine(min(start_dates_by_remote_uuid.values()), time.min)
        window_end = utc_now()
        totals_by_user: dict[str, int] = {user_uuid: 0 for user_uuid in start_dates_by_remote_uuid}
        node_usage_supported = False

        for node_uuid in whitelist_node_ids:
            rows = await self.remnawave.get_node_user_usage(node_uuid, ensure_utc(window_start), window_end)
            if rows is None:
                continue
            node_usage_supported = True
            for row in rows:
                row_user_uuid = str(row.userUuid)
                row_start_date = start_dates_by_remote_uuid.get(row_user_uuid)
                if row_start_date is None:
                    continue
                row_date = self._parse_usage_row_date(row.date)
                if row_date is None or row_date < row_start_date:
                    continue
                totals_by_user[row_user_uuid] += max(int(round(float(row.total))), 0)

        if node_usage_supported:
            return totals_by_user
        return {}

    @staticmethod
    def _sum_whitelist_usage_from_user_stats(usage, whitelist_node_ids: set[str]) -> int:
        total = 0
        for node in getattr(usage, "series", []) or []:
            if node.uuid not in whitelist_node_ids:
                continue
            total += max(int(round(float(node.total))), 0)
        if total > 0:
            return total

        for node in getattr(usage, "topNodes", []) or []:
            if node.uuid not in whitelist_node_ids:
                continue
            total += max(int(round(float(node.total))), 0)
        return total

    @staticmethod
    def _parse_usage_row_date(raw_value: str | None) -> date | None:
        if not raw_value:
            return None
        normalized = str(raw_value).strip()
        if not normalized:
            return None
        candidate = normalized.split("T", 1)[0]
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            return None

    def _compute_renewal_charge(
        self,
        subscription: Subscription,
        plan: Plan,
        *,
        plan_discount_rub: Decimal = Decimal("0.00"),
    ) -> Decimal:
        whitelist_charge = Decimal("0.00")
        if is_metered_plan_code(plan.code):
            whitelist_charge = self._compute_unbilled_whitelist_charge(subscription)
        discounted_plan_price = max(
            quantize_money(Decimal(plan.price_rub) - Decimal(plan_discount_rub)),
            Decimal("0.00"),
        )
        return quantize_money(discounted_plan_price + whitelist_charge)

    @staticmethod
    def _ensure_remote_expiration_synced(remote_user, expected_ends_at: datetime) -> None:
        if remote_user is None:
            return
        remote_expire_at = getattr(remote_user, "expireAt", None)
        if remote_expire_at is None:
            raise ConflictError("Remnawave не подтвердила новый срок подписки.")
        if ensure_utc(remote_expire_at) + timedelta(seconds=1) < ensure_utc(expected_ends_at):
            raise ConflictError("Remnawave вернула старый срок подписки после автопродления.")

    async def _apply_instant_whitelist_charges(self, user: User, subscription: Subscription) -> None:
        if subscription.plan is None or not is_metered_plan_code(subscription.plan.code):
            return

        used_bytes = max(int(subscription.whitelist_traffic_used_bytes or 0), 0)
        billed_bytes = max(int(subscription.whitelist_traffic_billed_bytes or 0), 0)
        if billed_bytes > used_bytes:
            billed_bytes = used_bytes
            subscription.whitelist_traffic_billed_bytes = billed_bytes

        outstanding_charge = self._compute_unbilled_whitelist_charge(subscription)
        if outstanding_charge <= Decimal("0.00"):
            return

        current_balance = Decimal(user.balance_rub)
        available_charge_room = quantize_money(current_balance - START_WHITELIST_BALANCE_FLOOR_RUB)
        if available_charge_room <= Decimal("0.00"):
            return

        requested_charge = min(outstanding_charge, available_charge_room)
        next_billed_bytes = self._advance_billed_whitelist_bytes(
            billed_bytes=billed_bytes,
            used_bytes=used_bytes,
            charge_cap_rub=requested_charge,
        )
        if next_billed_bytes <= billed_bytes:
            return

        applied_charge = quantize_money(
            self._whitelist_charge_for_bytes(next_billed_bytes) - self._whitelist_charge_for_bytes(billed_bytes)
        )
        if applied_charge <= Decimal("0.00"):
            return

        subscription.whitelist_traffic_billed_bytes = next_billed_bytes
        await self.accounts.adjust_balance(
            user_id=user.id,
            amount_rub=-applied_charge,
            transaction_type=BalanceTransactionType.SUBSCRIPTION_CHARGE,
            description="Моментальное списание за трафик белых списков по тарифу Start",
        )

    def _compute_unbilled_whitelist_charge(self, subscription: Subscription) -> Decimal:
        used_bytes = max(int(subscription.whitelist_traffic_used_bytes or 0), 0)
        billed_bytes = min(max(int(subscription.whitelist_traffic_billed_bytes or 0), 0), used_bytes)
        return quantize_money(
            self._whitelist_charge_for_bytes(used_bytes) - self._whitelist_charge_for_bytes(billed_bytes)
        )

    @staticmethod
    def _whitelist_charge_for_bytes(used_bytes: int) -> Decimal:
        return bytes_to_gb_cost(max(int(used_bytes or 0), 0), WHITELIST_GB_PRICE_RUB)

    def _advance_billed_whitelist_bytes(self, *, billed_bytes: int, used_bytes: int, charge_cap_rub: Decimal) -> int:
        if charge_cap_rub <= Decimal("0.00") or used_bytes <= billed_bytes:
            return billed_bytes

        starting_charge = self._whitelist_charge_for_bytes(billed_bytes)
        target_charge = quantize_money(starting_charge + Decimal(charge_cap_rub))
        low = billed_bytes
        high = used_bytes
        best = billed_bytes

        while low <= high:
            mid = (low + high) // 2
            mid_charge = self._whitelist_charge_for_bytes(mid)
            if mid_charge <= target_charge:
                best = mid
                low = mid + 1
            else:
                high = mid - 1

        return min(best, used_bytes)

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

    @staticmethod
    def _trial_reminder_window(remaining: timedelta) -> tuple[str, str] | None:
        if remaining <= timedelta(0):
            return None
        for reminder_key, upper_bound, lower_bound, reminder_label in TRIAL_REMINDER_WINDOWS:
            if lower_bound < remaining <= upper_bound:
                return reminder_key, reminder_label
        return None

    async def _promo_campaign_settings(self) -> dict[str, int | bool]:
        item = await self.session.scalar(
            select(SystemSetting).where(SystemSetting.key == PROMO_CAMPAIGN_SETTINGS_KEY)
        )
        raw_value = item.value if item is not None else None
        values = dict(DEFAULT_PROMO_CAMPAIGN_SETTINGS)
        if isinstance(raw_value, dict):
            for key in values:
                if key in raw_value:
                    values[key] = raw_value[key]
        return {
            "new_user_discount_percent": self._bounded_int(values["new_user_discount_percent"], 1, 100, 10),
            "lapsed_user_discount_percent": self._bounded_int(values["lapsed_user_discount_percent"], 1, 100, 35),
            "inactive_first_delay_days": self._bounded_int(values["inactive_first_delay_days"], 1, 365, 3),
            "lapsed_first_delay_days": self._bounded_int(values["lapsed_first_delay_days"], 1, 365, 1),
            "deep_winback_delay_days": self._bounded_int(values["deep_winback_delay_days"], 1, 365, 30),
            "return_trial_enabled": bool(values["return_trial_enabled"]),
            "return_trial_cooldown_days": self._bounded_int(values["return_trial_cooldown_days"], 1, 365, 30),
        }

    @staticmethod
    def _bounded_int(value: object, minimum: int, maximum: int, fallback: int) -> int:
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return fallback
        return min(max(parsed, minimum), maximum)

    @staticmethod
    def _select_campaign_template(user_id: str, campaign_key: str, options: tuple[int, ...]) -> int:
        if not options:
            return 1
        digest = hashlib.sha256(f"{user_id}:{campaign_key}".encode("utf-8")).hexdigest()
        return options[int(digest[:8], 16) % len(options)]

    async def _trial_period_for_user(self, user_id: str) -> TrialPeriod | None:
        return await self.session.scalar(select(TrialPeriod).where(TrialPeriod.user_id == user_id))

    async def _latest_paid_subscription_for_user(self, user_id: str) -> Subscription | None:
        return await self.session.scalar(
            select(Subscription)
            .join(Plan, Subscription.plan_id == Plan.id)
            .where(
                Subscription.user_id == user_id,
                Plan.is_trial.is_(False),
            )
            .order_by(Subscription.ends_at.desc(), Subscription.created_at.desc())
            .limit(1)
        )

    @staticmethod
    def _subscription_inactive_since(subscription: Subscription | None) -> datetime | None:
        if subscription is None:
            return None
        for value in (
            subscription.blocked_at,
            subscription.canceled_at,
            subscription.ends_at,
            subscription.updated_at,
        ):
            if value is not None:
                return ensure_utc(value)
        return None

    def _return_trial_allowed(
        self,
        trial_period: TrialPeriod | None,
        now: datetime,
        promo_settings: dict[str, int | bool],
    ) -> bool:
        if not bool(promo_settings["return_trial_enabled"]):
            return False
        if trial_period is None:
            return True
        if not trial_period.consumed:
            return True
        return ensure_utc(trial_period.ends_at) <= now - timedelta(
            days=int(promo_settings["return_trial_cooldown_days"])
        )

    @staticmethod
    def _choose_return_trial(user_id: str, campaign_key: str) -> bool:
        digest = hashlib.sha256(f"return-trial-choice:{user_id}:{campaign_key}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16) % 2 == 0

    async def _has_monthly_winback_contact(
        self,
        user_id: str,
        month_key: str,
        *,
        ignore_pending_dedupe_keys: set[str] | None = None,
    ) -> bool:
        legacy_and_current_keys = [
            f"inactive-promo:{user_id}:{month_key}",
            f"inactive-promo-lapsed:{user_id}:{month_key}",
            f"inactive-promo-lapsed-fresh:{user_id}:{month_key}",
            f"inactive-promo-lapsed-deep:{user_id}:{month_key}",
            f"inactive-promo-trial-deep:{user_id}:{month_key}",
            f"return-trial:{user_id}:{month_key}",
        ]
        month_start = datetime.fromisoformat(f"{month_key}-01T00:00:00+00:00")
        ignored_pending = ignore_pending_dedupe_keys or set()
        monthly_contacts = list(
            (
                await self.session.scalars(
                    select(Notification).where(
                        Notification.user_id == user_id,
                        or_(
                            Notification.dedupe_key.in_(legacy_and_current_keys),
                            Notification.created_at >= month_start,
                        ),
                    )
                )
            ).all()
        )
        for item in monthly_contacts:
            payload = item.payload if isinstance(item.payload, dict) else {}
            is_winback_contact = item.dedupe_key in legacy_and_current_keys or (
                ensure_utc(item.created_at) >= month_start
                and payload.get("cta") in {"inactive_promo", "trial_followup", "return_trial"}
            )
            if not is_winback_contact:
                continue
            if item.status == NotificationStatus.PENDING and item.dedupe_key in ignored_pending:
                continue
            return True
        return False

    async def _queue_inactive_user_promos(self, now: datetime) -> None:
        promo_settings = await self._promo_campaign_settings()
        registered_before = now - timedelta(days=int(promo_settings["inactive_first_delay_days"]))
        active_paid_subscription_exists = (
            select(Subscription.id)
            .join(Plan, Subscription.plan_id == Plan.id)
            .where(
                Subscription.user_id == User.id,
                Plan.is_trial.is_(False),
                Subscription.status.in_(
                    [
                        SubscriptionStatus.ACTIVE,
                        SubscriptionStatus.GRACE,
                    ]
                ),
            )
            .exists()
        )
        paid_subscription_history_exists = (
            select(Subscription.id)
            .join(Plan, Subscription.plan_id == Plan.id)
            .where(
                Subscription.user_id == User.id,
                Plan.is_trial.is_(False),
            )
            .exists()
        )
        users = list(
            (
                await self.session.execute(
                    select(
                        User,
                        paid_subscription_history_exists.label("has_paid_history"),
                    ).where(
                        User.registration_completed_at.is_not(None),
                        ~active_paid_subscription_exists,
                    )
                )
            ).all()
        )
        month_key = now.strftime("%Y-%m")
        for user, has_paid_history in users:
            campaign_kind = "lapsed" if has_paid_history else "new"
            trial_period = await self._trial_period_for_user(user.id)
            if await self.accounts.get_current_subscription(user.id) is not None:
                continue

            latest_paid_subscription = (
                await self._latest_paid_subscription_for_user(user.id)
                if has_paid_history
                else None
            )
            paid_inactive_since = self._subscription_inactive_since(latest_paid_subscription)
            paid_inactive_for = (
                now - paid_inactive_since
                if paid_inactive_since is not None
                else None
            )
            paid_is_deep_winback = bool(
                has_paid_history
                and paid_inactive_for is not None
                and paid_inactive_for >= timedelta(days=int(promo_settings["deep_winback_delay_days"]))
            )
            trial_is_deep_winback = bool(
                not has_paid_history
                and trial_period is not None
                and trial_period.consumed
                and ensure_utc(trial_period.ends_at)
                <= now - timedelta(days=int(promo_settings["deep_winback_delay_days"]))
            )

            if has_paid_history:
                if paid_inactive_for is None or paid_inactive_for < timedelta(
                    days=int(promo_settings["lapsed_first_delay_days"])
                ):
                    continue
            else:
                if user.registration_completed_at is None or ensure_utc(user.registration_completed_at) >= registered_before:
                    continue

            deep_campaign = paid_is_deep_winback or trial_is_deep_winback
            return_trial_ready = (
                deep_campaign
                and self._return_trial_allowed(trial_period, now, promo_settings)
                and self._choose_return_trial(user.id, f"{campaign_kind}:{month_key}")
            )
            if return_trial_ready:
                dedupe_key = f"return-trial:{user.id}:{month_key}"
                if await self._has_monthly_winback_contact(
                    user.id,
                    month_key,
                    ignore_pending_dedupe_keys={dedupe_key},
                ):
                    continue
                campaign_key = f"return-trial:{month_key}"
                template_id = self._select_campaign_template(
                    user.id,
                    campaign_key,
                    RETURN_TRIAL_TEMPLATE_IDS,
                )
                message = return_trial_offer_message(
                    template_id=template_id,
                    trial_days=int(self.settings.trial_duration_days or 2),
                )
                payload = {
                    "campaign": "return_trial_monthly",
                    "campaign_kind": "return_trial",
                    "template_id": template_id,
                    "trial_days": int(self.settings.trial_duration_days or 2),
                    "cta": "return_trial",
                    "parse_mode": "HTML",
                }
                notification = await self.notifications.queue(
                    user_id=user.id,
                    notification_type=NotificationType.BROADCAST,
                    message=message,
                    payload=payload,
                    dedupe_key=dedupe_key,
                )
                if notification.status == NotificationStatus.PENDING:
                    notification.message = message
                    notification.payload = payload
                continue
            if not has_paid_history and trial_period is not None and trial_period.consumed and not trial_is_deep_winback:
                continue

            discount_percent = (
                int(promo_settings["lapsed_user_discount_percent"])
                if deep_campaign
                else int(promo_settings["new_user_discount_percent"])
            )
            campaign_key = (
                f"inactive:{campaign_kind}:deep:{month_key}"
                if deep_campaign
                else f"inactive:{campaign_kind}:fresh:{month_key}"
            )
            promo = await self.promos.get_or_create_personal_discount_code(
                user.id,
                discount_percent=Decimal(discount_percent),
                campaign_key=campaign_key,
            )
            template_id = self._select_campaign_template(
                user.id,
                campaign_key,
                DISCOUNT_PROMO_TEMPLATE_IDS,
            )
            message = inactive_subscription_promo_message(
                promo.code,
                discount_percent,
                template_id=template_id,
            )
            payload = {
                "promo_code": promo.code,
                "discount_percent": discount_percent,
                "campaign": "inactive_monthly",
                "campaign_kind": f"{campaign_kind}_deep" if deep_campaign else f"{campaign_kind}_fresh",
                "template_id": template_id,
                "cta": "inactive_promo",
                "parse_mode": "HTML",
            }
            dedupe_key = (
                f"inactive-promo-lapsed-deep:{user.id}:{month_key}"
                if has_paid_history and deep_campaign
                else f"inactive-promo-lapsed-fresh:{user.id}:{month_key}"
                if has_paid_history
                else f"inactive-promo-trial-deep:{user.id}:{month_key}"
                if deep_campaign
                else f"inactive-promo:{user.id}:{month_key}"
            )
            if await self._has_monthly_winback_contact(
                user.id,
                month_key,
                ignore_pending_dedupe_keys={dedupe_key},
            ):
                continue
            notification = await self.notifications.queue(
                user_id=user.id,
                notification_type=NotificationType.PROMO_CODE,
                message=message,
                payload=payload,
                dedupe_key=dedupe_key,
            )
            if notification.status == NotificationStatus.PENDING:
                notification.message = message
                notification.payload = payload

    async def _queue_post_trial_followups(self, now: datetime) -> None:
        promo_settings = await self._promo_campaign_settings()
        discount_percent = int(promo_settings["new_user_discount_percent"])
        month_key = now.strftime("%Y-%m")
        followup_ready_at = now - timedelta(hours=12)
        subscriptions = list(
            (
                await self.session.scalars(
                    select(Subscription)
                    .join(Plan, Subscription.plan_id == Plan.id)
                    .options(joinedload(Subscription.user), joinedload(Subscription.plan))
                    .where(
                        Subscription.status == SubscriptionStatus.EXPIRED,
                        Subscription.ends_at <= followup_ready_at,
                        Plan.is_trial.is_(True),
                    )
                    .order_by(Subscription.ends_at.desc())
                )
            ).all()
        )
        latest_trial_by_user: dict[str, Subscription] = {}
        for subscription in subscriptions:
            if subscription.user_id not in latest_trial_by_user:
                latest_trial_by_user[subscription.user_id] = subscription

        for subscription in latest_trial_by_user.values():
            user = subscription.user
            if user is None:
                continue
            if await self.accounts.has_paid_subscription_history(user.id):
                continue
            if await self.accounts.get_current_subscription(user.id) is not None:
                continue
            dedupe_key = f"trial-followup:{subscription.id}:12h"
            if await self._has_monthly_winback_contact(
                user.id,
                month_key,
                ignore_pending_dedupe_keys={dedupe_key},
            ):
                continue
            trial_period = await self._trial_period_for_user(user.id)
            if (
                bool(promo_settings["return_trial_enabled"])
                and trial_period is not None
                and trial_period.consumed
                and ensure_utc(trial_period.ends_at)
                <= now - timedelta(days=int(promo_settings["deep_winback_delay_days"]))
            ):
                continue
            promo = await self.promos.get_or_create_personal_discount_code(
                user.id,
                discount_percent=Decimal(discount_percent),
                campaign_key=f"trial-followup:{subscription.id}:12h",
            )
            message = trial_followup_message(promo.code, discount_percent)
            payload = {
                "promo_code": promo.code,
                "discount_percent": discount_percent,
                "kind": "trial_followup",
                "cta": "trial_followup",
                "parse_mode": "HTML",
            }
            notification = await self.notifications.queue(
                user_id=user.id,
                notification_type=NotificationType.BROADCAST,
                message=message,
                payload=payload,
                dedupe_key=dedupe_key,
            )
            if notification.status == NotificationStatus.PENDING:
                notification.message = message
                notification.payload = payload

    async def _sync_trial_subscription_state(self, subscription: Subscription, now: datetime) -> bool:
        user = subscription.user
        if user is None:
            return False

        trial_ends_at = ensure_utc(subscription.ends_at)
        if trial_ends_at > now:
            await self._queue_trial_setup_help_if_needed(subscription, user, now)

        trial_reminder_window = self._trial_reminder_window(trial_ends_at - now)
        if trial_reminder_window is not None:
            reminder_key, reminder_label = trial_reminder_window
            await self.notifications.queue(
                user_id=user.id,
                notification_type=NotificationType.BROADCAST,
                message=trial_expiring_message(trial_ends_at, reminder_label),
                payload={"kind": "trial_expiring", "window": reminder_key, "cta": "trial_expiring"},
                dedupe_key=f"trial-reminder:{subscription.id}:{reminder_key}",
            )

        if trial_ends_at > now:
            return False

        subscription.status = SubscriptionStatus.EXPIRED
        user.status = UserStatus.BLOCKED
        await self._disable_remote_user_best_effort(user, event_type="trial_disable_failed")
        await self.notifications.queue(
            user_id=user.id,
            notification_type=NotificationType.TRIAL_ENDED,
            message=trial_ended_message(),
            payload={"kind": "trial_ended", "cta": "trial_ended"},
            dedupe_key=f"trial-ended:{subscription.id}",
        )
        return True

    async def _queue_trial_setup_help_if_needed(self, subscription: Subscription, user: User, now: datetime) -> None:
        started_at = ensure_utc(subscription.started_at)
        if now < started_at + TRIAL_SETUP_HELP_DELAY:
            return
        if await self._trial_has_connection_activity(subscription, started_at):
            return

        support_username = self.settings.support_username or "@altlink_support"
        support_handle = support_username if support_username.startswith("@") else f"@{support_username}"
        support_url = f"https://t.me/{support_handle.lstrip('@')}"
        await self.notifications.queue(
            user_id=user.id,
            notification_type=NotificationType.BROADCAST,
            message=trial_setup_help_message(support_handle),
            payload={
                "kind": "trial_setup_help",
                "cta": "trial_setup_help",
                "support_username": support_handle,
                "support_url": support_url,
            },
            dedupe_key=f"trial-setup-help:{subscription.id}:3h",
        )

    async def _trial_has_connection_activity(
        self,
        subscription: Subscription,
        started_at: datetime,
    ) -> bool:
        if int(subscription.traffic_used_bytes or 0) > 0 or int(subscription.whitelist_traffic_used_bytes or 0) > 0:
            return True

        online_activity_id = await self.session.scalar(
            select(OnlineSessionCache.id)
            .where(
                OnlineSessionCache.user_id == subscription.user_id,
                OnlineSessionCache.last_activity_at.is_not(None),
                OnlineSessionCache.last_activity_at >= started_at,
            )
            .limit(1)
        )
        if online_activity_id is not None:
            return True

        traffic_snapshot_id = await self.session.scalar(
            select(TrafficSnapshot.id)
            .where(
                TrafficSnapshot.user_id == subscription.user_id,
                TrafficSnapshot.created_at >= started_at,
                or_(TrafficSnapshot.used_bytes > 0, TrafficSnapshot.lifetime_used_bytes > 0),
            )
            .limit(1)
        )
        return traffic_snapshot_id is not None

    async def _disable_remote_user_best_effort(self, user: User, *, event_type: str) -> None:
        if self.remnawave is None or not user.remnawave_user_uuid:
            return
        try:
            await self.remnawave.disable_user(user.remnawave_user_uuid)
        except httpx.HTTPStatusError as exc:
            await self.log_event(
                level=SystemEventLevel.WARNING,
                event_type=event_type,
                message="Не удалось отключить пользователя в панели при обновлении статуса подписки.",
                payload={
                    "user_id": user.id,
                    "telegram_id": user.telegram_id,
                    "remnawave_user_uuid": user.remnawave_user_uuid,
                    "error": self._format_remnawave_error(exc),
                },
            )
        except httpx.HTTPError as exc:
            await self.log_event(
                level=SystemEventLevel.WARNING,
                event_type=event_type,
                message="Не удалось отключить пользователя в панели при обновлении статуса подписки.",
                payload={
                    "user_id": user.id,
                    "telegram_id": user.telegram_id,
                    "remnawave_user_uuid": user.remnawave_user_uuid,
                    "error": str(exc),
                },
            )

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

    async def _sync_user_remote_access(
        self,
        user: User,
        subscription: Subscription,
        plan: Plan,
        *,
        enable: bool,
        reset_traffic: bool,
    ):
        try:
            await self.catalog.sync_user_target_squads(user.id)
        except httpx.HTTPStatusError as exc:
            raise ConflictError(self._format_remnawave_error(exc)) from exc
        except httpx.HTTPError as exc:
            raise ConflictError("Не удалось связаться с панелью для активации доступа. Попробуйте позже.") from exc

        return await self._sync_remote_state(
            user,
            subscription,
            plan,
            enable=enable,
            reset_traffic=reset_traffic,
        )

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
            and self.catalog.is_server_usable(access.server)
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
                try:
                    remote = await self.remnawave.update_user(payload)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
                    payload.pop("uuid", None)
                    remote = await self.remnawave.create_user(payload)
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
        except httpx.HTTPError as exc:
            raise ConflictError("Не удалось связаться с панелью для активации доступа. Попробуйте позже.") from exc

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
