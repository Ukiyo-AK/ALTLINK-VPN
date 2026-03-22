from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from altlink.application.services import AccountService, AdminAuthService, BillingService, SubscriptionService
from altlink.domain.enums import SubscriptionStatus, TopupRequestStatus, UserStatus
from altlink.infrastructure.db.models import BalanceTransaction, Subscription, TopupRequest, User


@pytest.mark.asyncio
async def test_trial_activation_creates_subscription_and_remote_user(session_factory, test_settings, fake_remnawave):
    remnawave = fake_remnawave
    async with session_factory() as session:
        account = AccountService(session, test_settings, remnawave)
        user = await account.register_or_update_telegram_user(
            telegram_id=1001,
            username="tester",
            first_name="Test",
            last_name=None,
            language_code="ru",
        )
        await SubscriptionService(session, test_settings, remnawave).activate_trial(user)
        await session.commit()

    async with session_factory() as session:
        db_user = (await session.execute(select(User).where(User.telegram_id == 1001))).scalar_one()
        subscription = (
            await session.execute(select(Subscription).where(Subscription.user_id == db_user.id))
        ).scalar_one()
        assert db_user.status == UserStatus.TRIAL
        assert db_user.remnawave_user_uuid is not None
        assert subscription.status == SubscriptionStatus.TRIAL
        assert remnawave.created_users


@pytest.mark.asyncio
async def test_topup_approval_updates_balance_and_transaction(session_factory, test_settings, fake_remnawave):
    remnawave = fake_remnawave
    async with session_factory() as session:
        admin = await AdminAuthService(session, test_settings, remnawave).create_or_update_admin(
            username="root",
            password="strong-password",
            telegram_id=999,
            full_name="Root",
        )
        user = await AccountService(session, test_settings, remnawave).register_or_update_telegram_user(
            telegram_id=1002,
            username="buyer",
            first_name="Buyer",
            last_name=None,
            language_code="ru",
        )
        topup = await BillingService(session, test_settings, remnawave).create_topup_request(
            user, Decimal("300.00")
        )
        await BillingService(session, test_settings, remnawave).approve_topup_request(topup, admin)
        await session.commit()

    async with session_factory() as session:
        db_user = (await session.execute(select(User).where(User.telegram_id == 1002))).scalar_one()
        db_topup = (await session.execute(select(TopupRequest).where(TopupRequest.user_id == db_user.id))).scalar_one()
        tx = (await session.execute(select(BalanceTransaction).where(BalanceTransaction.user_id == db_user.id))).scalar_one()
        assert db_user.balance_rub == Decimal("300.00")
        assert db_topup.status == TopupRequestStatus.APPROVED
        assert tx.amount_rub == Decimal("300.00")


@pytest.mark.asyncio
async def test_due_subscription_enters_grace_when_balance_missing(session_factory, test_settings, fake_remnawave):
    remnawave = fake_remnawave
    async with session_factory() as session:
        user = await AccountService(session, test_settings, remnawave).register_or_update_telegram_user(
            telegram_id=1003,
            username="debtor",
            first_name="Debtor",
            last_name=None,
            language_code="ru",
        )
        await SubscriptionService(session, test_settings, remnawave).activate_paid_plan(
            user,
            "unlimited_30d",
            charge_immediately=False,
        )
        subscription = (await session.execute(select(Subscription).where(Subscription.user_id == user.id))).scalar_one()
        subscription.next_billing_at = datetime.now(UTC) - timedelta(minutes=1)
        subscription.renewal_price_rub = Decimal("200.00")
        user.balance_rub = Decimal("0.00")
        await BillingService(session, test_settings, remnawave).process_due_subscriptions()
        await session.commit()

    async with session_factory() as session:
        db_user = (await session.execute(select(User).where(User.telegram_id == 1003))).scalar_one()
        db_subscription = (await session.execute(select(Subscription).where(Subscription.user_id == db_user.id))).scalar_one()
        assert db_user.status == UserStatus.GRACE
        assert db_subscription.status == SubscriptionStatus.GRACE
        assert db_subscription.debt_rub == Decimal("200.00")
