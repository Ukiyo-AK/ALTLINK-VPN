from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import desc, select

from altlink.application.services.base import ServiceBase
from altlink.application.services.notifications import NotificationService
from altlink.domain.enums import (
    BalanceTransactionType,
    EventLevel,
    NotificationType,
    SubscriptionStatus,
    TopupRequestStatus,
    TrialStatus,
    UserStatus,
)
from altlink.infrastructure.db.models import (
    AdminUser,
    BalanceTransaction,
    Plan,
    Subscription,
    TopupRequest,
    TrialPeriod,
    User,
)


class BillingService(ServiceBase):
    async def create_topup_request(self, user: User, amount_rub: Decimal, comment: str | None = None) -> TopupRequest:
        request = TopupRequest(
            user_id=user.id,
            amount_rub=amount_rub,
            user_comment=comment,
        )
        self.session.add(request)
        await self.session.flush()
        await self.log_event(
            scope="billing",
            level=EventLevel.INFO,
            title="Создана заявка на пополнение",
            user_id=user.id,
            payload={"amount_rub": str(amount_rub)},
        )
        return request

    async def approve_topup_request(
        self,
        request: TopupRequest,
        admin: AdminUser,
        *,
        comment: str | None = None,
    ) -> TopupRequest:
        if request.status != TopupRequestStatus.NEW:
            raise ValueError("Заявка уже обработана")
        user = await self.session.get(User, request.user_id)
        if user is None:
            raise ValueError("Пользователь не найден")
        before = user.balance_rub
        user.balance_rub += request.amount_rub
        request.status = TopupRequestStatus.APPROVED
        request.admin_comment = comment
        request.approved_by_admin_id = admin.id
        request.approved_at = datetime.now(UTC)
        request.processed_at = request.approved_at
        self.session.add(
            BalanceTransaction(
                user_id=user.id,
                topup_request_id=request.id,
                admin_user_id=admin.id,
                transaction_type=BalanceTransactionType.TOPUP,
                amount_rub=request.amount_rub,
                balance_before=before,
                balance_after=user.balance_rub,
                comment=comment or "Заявка на пополнение подтверждена администратором",
            )
        )
        notifier = NotificationService(self.session, self.settings, self.remnawave)
        await notifier.queue(
            user=user,
            notification_type=NotificationType.TOPUP_APPROVED,
            title="Пополнение подтверждено",
            message=f"Ваше пополнение на {request.amount_rub:.2f} ₽ подтверждено. Текущий баланс: {user.balance_rub:.2f} ₽.",
            dedupe_key=f"topup-approved:{request.id}",
        )
        await self.log_event(
            scope="billing",
            level=EventLevel.INFO,
            title="Пополнение подтверждено",
            user_id=user.id,
            payload={"topup_request_id": request.id},
        )
        await self.try_recover_subscription(user)
        return request

    async def reject_topup_request(
        self,
        request: TopupRequest,
        admin: AdminUser,
        *,
        comment: str | None = None,
    ) -> TopupRequest:
        if request.status != TopupRequestStatus.NEW:
            raise ValueError("Заявка уже обработана")
        request.status = TopupRequestStatus.REJECTED
        request.admin_comment = comment
        request.approved_by_admin_id = admin.id
        request.rejected_at = datetime.now(UTC)
        request.processed_at = request.rejected_at
        user = await self.session.get(User, request.user_id)
        if user is not None:
            notifier = NotificationService(self.session, self.settings, self.remnawave)
            await notifier.queue(
                user=user,
                notification_type=NotificationType.TOPUP_REJECTED,
                title="Пополнение отклонено",
                message="Заявка на пополнение отклонена администратором. Если это ошибка, свяжитесь с поддержкой.",
                dedupe_key=f"topup-rejected:{request.id}",
            )
        await self.log_event(
            scope="billing",
            level=EventLevel.WARNING,
            title="Пополнение отклонено",
            user_id=request.user_id,
            payload={"topup_request_id": request.id},
        )
        return request

    async def cancel_topup_request(self, request: TopupRequest) -> TopupRequest:
        if request.status != TopupRequestStatus.NEW:
            raise ValueError("Отменить можно только новую заявку")
        request.status = TopupRequestStatus.CANCELED
        request.canceled_at = datetime.now(UTC)
        request.processed_at = request.canceled_at
        return request

    async def adjust_balance(
        self,
        user: User,
        admin: AdminUser,
        *,
        amount_rub: Decimal,
        comment: str,
    ) -> BalanceTransaction:
        before = user.balance_rub
        user.balance_rub += amount_rub
        transaction = BalanceTransaction(
            user_id=user.id,
            admin_user_id=admin.id,
            transaction_type=BalanceTransactionType.MANUAL_ADJUSTMENT,
            amount_rub=amount_rub,
            balance_before=before,
            balance_after=user.balance_rub,
            comment=comment,
        )
        self.session.add(transaction)
        await self.log_event(
            scope="billing",
            level=EventLevel.INFO,
            title="Баланс скорректирован администратором",
            user_id=user.id,
            payload={"amount_rub": str(amount_rub)},
        )
        await self.try_recover_subscription(user)
        return transaction

    async def try_recover_subscription(self, user: User) -> bool:
        subscription = await self.get_current_subscription(user.id)
        if subscription is None or subscription.status != SubscriptionStatus.GRACE:
            return False
        if user.balance_rub < subscription.debt_rub:
            return False
        plan = await self.session.get(Plan, subscription.plan_id)
        if plan is None:
            return False

        before = user.balance_rub
        user.balance_rub -= subscription.debt_rub
        self.session.add(
            BalanceTransaction(
                user_id=user.id,
                subscription_id=subscription.id,
                transaction_type=BalanceTransactionType.DEBT_SETTLEMENT,
                amount_rub=-subscription.debt_rub,
                balance_before=before,
                balance_after=user.balance_rub,
                comment="Погашение задолженности в grace period",
            )
        )
        anchor = subscription.current_period_end or datetime.now(UTC)
        subscription.current_period_start = anchor
        subscription.current_period_end = anchor + timedelta(days=plan.duration_days)
        subscription.next_billing_at = subscription.current_period_end
        subscription.grace_started_at = None
        subscription.grace_ends_at = None
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.last_traffic_reset_at = datetime.now(UTC)
        subscription.traffic_used_bytes_cache = 0
        subscription.debt_rub = Decimal("0.00")
        user.status = UserStatus.ACTIVE
        if user.remnawave_user_uuid and self.remnawave:
            await self.remnawave.enable_user(user.remnawave_user_uuid)
            await self.remnawave.update_user(
                uuid=user.remnawave_user_uuid,
                expire_at=subscription.current_period_end
                + timedelta(days=self.settings.grace_period_days),
                traffic_limit_bytes=plan.traffic_limit_bytes or 0,
            )
            if plan.traffic_limit_bytes:
                await self.remnawave.reset_user_traffic(user.remnawave_user_uuid)
        await self.log_event(
            scope="billing",
            level=EventLevel.INFO,
            title="Подписка восстановлена из grace period",
            user_id=user.id,
            subscription_id=subscription.id,
        )
        return True

    async def process_due_subscriptions(self) -> dict[str, int]:
        now = datetime.now(UTC)
        result = await self.session.execute(
            select(Subscription)
            .where(Subscription.is_current.is_(True), Subscription.next_billing_at.is_not(None))
            .order_by(Subscription.next_billing_at.asc())
        )
        subscriptions = result.scalars().all()
        stats = {"renewed": 0, "grace_started": 0, "blocked": 0, "trial_finished": 0}
        notifier = NotificationService(self.session, self.settings, self.remnawave)
        for subscription in subscriptions:
            if subscription.next_billing_at is None or subscription.next_billing_at > now:
                continue
            user = await self.session.get(User, subscription.user_id)
            if user is None:
                continue
            plan = await self.session.get(Plan, subscription.plan_id)
            if plan is None:
                continue

            if subscription.status == SubscriptionStatus.TRIAL:
                subscription.status = SubscriptionStatus.EXPIRED
                user.status = UserStatus.BLOCKED
                stats["trial_finished"] += 1
                trial = (
                    await self.session.execute(
                        select(TrialPeriod)
                        .where(TrialPeriod.user_id == user.id)
                        .order_by(desc(TrialPeriod.created_at))
                    )
                ).scalar_one_or_none()
                if trial is not None:
                    trial.status = TrialStatus.EXPIRED
                    trial.ended_at = now
                if user.remnawave_user_uuid and self.remnawave:
                    await self.remnawave.disable_user(user.remnawave_user_uuid)
                await notifier.queue(
                    user=user,
                    notification_type=NotificationType.TRIAL_ENDED,
                    title="Тестовый период завершен",
                    message="Тестовый период завершился. Выберите тариф и пополните баланс, чтобы продолжить пользоваться VPN.",
                    dedupe_key=f"trial-ended:{subscription.id}",
                )
                continue

            if subscription.status == SubscriptionStatus.GRACE:
                if user.balance_rub >= subscription.debt_rub:
                    recovered = await self.try_recover_subscription(user)
                    stats["renewed"] += 1 if recovered else 0
                    continue
                if subscription.grace_ends_at and subscription.grace_ends_at <= now:
                    subscription.status = SubscriptionStatus.BLOCKED
                    subscription.blocked_at = now
                    user.status = UserStatus.BLOCKED
                    stats["blocked"] += 1
                    if user.remnawave_user_uuid and self.remnawave:
                        await self.remnawave.disable_user(user.remnawave_user_uuid)
                    await notifier.queue(
                        user=user,
                        notification_type=NotificationType.ACCESS_BLOCKED,
                        title="Доступ заблокирован",
                        message="Льготный период завершился, доступ к VPN временно заблокирован. Пополните баланс для восстановления.",
                        dedupe_key=f"blocked:{subscription.id}:{now.date().isoformat()}",
                    )
                else:
                    await notifier.queue(
                        user=user,
                        notification_type=NotificationType.GRACE_REMINDER,
                        title="Нужно пополнить баланс",
                        message=f"У вас действует льготный период. Задолженность: {subscription.debt_rub:.2f} ₽. Пополните баланс, чтобы не потерять доступ.",
                        dedupe_key=f"grace-reminder:{subscription.id}:{now.date().isoformat()}",
                    )
                continue

            if subscription.status != SubscriptionStatus.ACTIVE:
                continue

            if user.balance_rub >= subscription.renewal_price_rub:
                before = user.balance_rub
                user.balance_rub -= subscription.renewal_price_rub
                self.session.add(
                    BalanceTransaction(
                        user_id=user.id,
                        subscription_id=subscription.id,
                        transaction_type=BalanceTransactionType.RENEWAL,
                        amount_rub=-subscription.renewal_price_rub,
                        balance_before=before,
                        balance_after=user.balance_rub,
                        comment=f"Автопродление тарифа {plan.name_ru}",
                    )
                )
                anchor = subscription.current_period_end or now
                subscription.current_period_start = anchor
                subscription.current_period_end = anchor + timedelta(days=plan.duration_days)
                subscription.next_billing_at = subscription.current_period_end
                subscription.debt_rub = Decimal("0.00")
                subscription.last_traffic_reset_at = now
                subscription.traffic_used_bytes_cache = 0
                user.status = UserStatus.ACTIVE
                if user.remnawave_user_uuid and self.remnawave:
                    await self.remnawave.enable_user(user.remnawave_user_uuid)
                    await self.remnawave.update_user(
                        uuid=user.remnawave_user_uuid,
                        expire_at=subscription.current_period_end
                        + timedelta(days=self.settings.grace_period_days),
                        traffic_limit_bytes=plan.traffic_limit_bytes or 0,
                    )
                    if plan.traffic_limit_bytes:
                        await self.remnawave.reset_user_traffic(user.remnawave_user_uuid)
                stats["renewed"] += 1
            else:
                subscription.status = SubscriptionStatus.GRACE
                subscription.grace_started_at = now
                subscription.grace_ends_at = now + timedelta(days=self.settings.grace_period_days)
                subscription.next_billing_at = subscription.grace_ends_at
                subscription.debt_rub = subscription.renewal_price_rub
                user.status = UserStatus.GRACE
                if user.remnawave_user_uuid and self.remnawave:
                    await self.remnawave.update_user(
                        uuid=user.remnawave_user_uuid,
                        expire_at=subscription.grace_ends_at,
                        traffic_limit_bytes=plan.traffic_limit_bytes or 0,
                    )
                    await self.remnawave.enable_user(user.remnawave_user_uuid)
                stats["grace_started"] += 1
                await notifier.queue(
                    user=user,
                    notification_type=NotificationType.GRACE_STARTED,
                    title="Начался льготный период",
                    message=f"На балансе недостаточно средств для продления. VPN продолжит работать еще {self.settings.grace_period_days} дней, задолженность: {subscription.debt_rub:.2f} ₽.",
                    dedupe_key=f"grace-started:{subscription.id}:{subscription.grace_started_at.date().isoformat()}",
                )
        return stats

    async def queue_prebilling_and_low_balance_notifications(self) -> int:
        now = datetime.now(UTC)
        notifier = NotificationService(self.session, self.settings, self.remnawave)
        result = await self.session.execute(
            select(Subscription, User, Plan)
            .join(User, User.id == Subscription.user_id)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(
                Subscription.is_current.is_(True),
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.next_billing_at.is_not(None),
            )
        )
        count = 0
        for subscription, user, plan in result.all():
            if subscription.next_billing_at is None:
                continue
            delta = subscription.next_billing_at - now
            if 0 < delta.days <= self.settings.low_balance_notify_days:
                await notifier.queue(
                    user=user,
                    notification_type=NotificationType.UPCOMING_RENEWAL,
                    title="Скоро списание",
                    message=f"Следующее списание за тариф «{plan.name_ru}» произойдет {subscription.next_billing_at:%d.%m.%Y %H:%M}. Подготовьте {subscription.renewal_price_rub:.2f} ₽ на балансе.",
                    dedupe_key=f"renewal-reminder:{subscription.id}:{subscription.next_billing_at.date().isoformat()}",
                )
                count += 1
            if user.balance_rub < Decimal(str(self.settings.low_balance_threshold_rub)):
                await notifier.queue(
                    user=user,
                    notification_type=NotificationType.LOW_BALANCE,
                    title="Низкий баланс",
                    message=f"Баланс ниже рекомендуемого порога: {user.balance_rub:.2f} ₽. Пополните счет заранее, чтобы избежать перехода в льготный период.",
                    dedupe_key=f"low-balance:{subscription.id}:{now.date().isoformat()}",
                )
                count += 1
        return count
