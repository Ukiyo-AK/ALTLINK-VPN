from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import runpy

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import select

from altlink.domain.enums import NotificationStatus, NotificationType, PlanCode, SubscriptionStatus
from altlink.infrastructure.db.models import BalanceTransaction, Notification, Subscription, User
from altlink.utils.time import ensure_utc, utc_now


async def migrate(hub, *, downgrade=False):
    module = runpy.run_path(str(
        Path(__file__).resolve().parents[2]
        / "alembic/versions/20260914_0019_enable_paid_subscription_renewal.py"
    ))
    await hub.session.flush()
    connection = await hub.session.connection()

    def run(bind):
        with Operations.context(MigrationContext.configure(bind)):
            module["downgrade" if downgrade else "upgrade"]()

    await connection.run_sync(run)


async def create_user(hub, telegram_id):
    return await hub.accounts.get_or_create_user(
        telegram_id=telegram_id, username=f"renewal_{telegram_id}",
        first_name="Renewal", last_name="Test", language_code="ru",
    )


async def snapshot(hub, model):
    return {
        row["id"]: dict(row)
        for row in (await hub.session.execute(select(model.__table__))).mappings()
    }


@pytest.mark.asyncio
async def test_migration_enables_paid_flags_without_charges_or_reactivation(test_services):
    async with test_services.hub() as hub:
        for index, status in enumerate([
            SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE,
            SubscriptionStatus.BLOCKED, SubscriptionStatus.CANCELED,
        ]):
            user = await create_user(hub, 91000 + index)
            user.balance_rub = Decimal("300")
            subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED)
            subscription.auto_renew = False
            subscription.status = status
            if status == SubscriptionStatus.GRACE:
                subscription.grace_until = subscription.ends_at
        trial_user = await create_user(hub, 91010)
        await hub.billing.activate_trial(trial_user.id)
        await create_user(hub, 91011)  # Registration alone must not activate a plan.
        await hub.session.flush()
        before_subscriptions = await snapshot(hub, Subscription)
        before_users = await snapshot(hub, User)
        before_transactions = await snapshot(hub, BalanceTransaction)
        remote_users = dict(test_services.remnawave.users)

        await migrate(hub)
        await migrate(hub)  # Re-running the data operation is harmless.
        await migrate(hub, downgrade=True)  # No blanket disable on rollback.

        expected = {
            key: {**row, "auto_renew": row["status"] != SubscriptionStatus.TRIAL}
            for key, row in before_subscriptions.items()
        }
        assert await snapshot(hub, Subscription) == expected
        assert await snapshot(hub, User) == before_users
        assert await snapshot(hub, BalanceTransaction) == before_transactions
        assert test_services.remnawave.users == remote_users


@pytest.mark.asyncio
@pytest.mark.parametrize("balance", [Decimal("0"), Decimal("300")])
async def test_migrated_subscription_uses_regular_billing_and_remote_sync(test_services, balance):
    async with test_services.hub() as hub:
        user = await create_user(hub, 91100)
        user.balance_rub = Decimal("500")
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED)
        plan_price = Decimal(subscription.plan.price_rub)
        subscription.auto_renew = False
        subscription.ends_at = utc_now() - timedelta(minutes=1)
        subscription.next_billing_at = subscription.ends_at
        user.balance_rub = balance
        await migrate(hub)
        await hub.session.refresh(subscription)
        assert subscription.auto_renew is True
        assert Decimal(user.balance_rub) == balance

        await hub.billing.process_due_subscriptions()
        await hub.billing.process_due_subscriptions()
        if balance:
            assert subscription.status == SubscriptionStatus.ACTIVE
            assert Decimal(user.balance_rub) == balance - plan_price
            assert ensure_utc(test_services.remnawave.users[user.remnawave_user_uuid].expireAt) > utc_now()
        else:
            assert subscription.status == SubscriptionStatus.BLOCKED
            assert Decimal(user.balance_rub) == balance
            notification = await hub.session.scalar(select(Notification).where(Notification.dedupe_key == f"blocked:{subscription.id}"))
            assert notification is not None
        notifications = list((await hub.session.scalars(select(Notification))).all())
        assert not any("автопродлен" in item.message.casefold() for item in notifications)


@pytest.mark.asyncio
async def test_migration_retires_only_pending_legacy_copy(test_services):
    samples = [
        (NotificationType.BROADCAST, "renewal_disabled_expiring", "Автопродление сейчас отключено."),
        (NotificationType.BROADCAST, "subscription_ended_auto_renew_disabled", "Автопродление было отключено, поэтому доступ остановлен.\nНажмите «Включить автопродление», чтобы снова активировать этот тариф."),
        (NotificationType.TOPUP_APPROVED, "topup_enable_auto_renew", "Зачислено 100 ₽.\n\nАвтопродление подписки отключено. Включите его, чтобы доступ продолжился после окончания тарифа."),
        (NotificationType.TOPUP_APPROVED, "topup_resume_subscription", "Зачислено 100 ₽.\n\nПополнение не возобновляет тариф «Pro» автоматически, потому что автопродление было отключено. Возобновите тариф, чтобы вернуть доступ."),
        (NotificationType.LOW_BALANCE, "low_balance", "⚠️ Не хватает средств для автопродления.\nБаланс: 10 ₽"),
    ]
    async with test_services.hub() as hub:
        user = await create_user(hub, 91200)
        user.balance_rub = Decimal("500")
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED)
        subscription.notes = "Автопродление с промокодом ABCD1234: скидка 19.90 ₽."
        for status in (NotificationStatus.PENDING, NotificationStatus.SENT):
            for notification_type, cta, message in samples:
                hub.session.add(Notification(
                    user_id=user.id, type=notification_type, status=status,
                    message=message, payload={"cta": cta}, dedupe_key=f"legacy:{status}:{cta}",
                ))
        await hub.session.flush()
        sent_before = {
            key: row for key, row in (await snapshot(hub, Notification)).items()
            if row["status"] == NotificationStatus.SENT
        }
        await migrate(hub)
        after = await snapshot(hub, Notification)
        await migrate(hub)
        assert await snapshot(hub, Notification) == after
        for key, row in after.items():
            if key in sent_before:
                assert row == sent_before[key]
            elif row["payload"]["cta"] == "renewal_disabled_expiring":
                assert row["status"] == NotificationStatus.FAILED
                assert "Superseded" in row["failure_reason"]
            else:
                assert row["status"] == NotificationStatus.PENDING
                assert "автопродлен" not in row["message"].casefold()
        await hub.session.refresh(subscription)
        assert subscription.notes == "Продление с промокодом ABCD1234: скидка 19.90 ₽."
