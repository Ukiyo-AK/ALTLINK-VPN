from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from altlink.domain.enums import BalanceTransactionType, PlanCode, SubscriptionStatus, UserStatus
from altlink.utils.time import utc_now


@pytest.mark.asyncio
async def test_monthly_renewal_blocks_user_without_balance(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=4001,
            username="renewal_block",
            first_name="Renewal",
            last_name="Block",
            language_code="ru",
        )
        await hub.accounts.adjust_balance(
            user_id=user.id,
            amount_rub=Decimal("250"),
            transaction_type=BalanceTransactionType.TOPUP,
            description="Seed balance",
        )
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED, charge_user=True)
        subscription.next_billing_at = utc_now() - timedelta(minutes=1)
        user.balance_rub = Decimal("0")

    async with test_services.hub() as hub:
        await hub.billing.process_due_subscriptions()

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(4001)
        latest = await hub.accounts.get_latest_subscription(user.id)
        assert user.status == UserStatus.BLOCKED
        assert latest.status == SubscriptionStatus.BLOCKED


@pytest.mark.asyncio
async def test_disabled_auto_renew_cancels_subscription_at_due_date(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=4002,
            username="renewal_off",
            first_name="Renewal",
            last_name="Off",
            language_code="ru",
        )
        await hub.accounts.adjust_balance(
            user_id=user.id,
            amount_rub=Decimal("500"),
            transaction_type=BalanceTransactionType.TOPUP,
            description="Seed balance",
        )
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        await hub.billing.cancel_subscription_renewal(user.id)
        subscription.next_billing_at = utc_now() - timedelta(minutes=1)

    async with test_services.hub() as hub:
        await hub.billing.process_due_subscriptions()

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(4002)
        latest = await hub.accounts.get_latest_subscription(user.id)
        assert user.status == UserStatus.CANCELED
        assert latest.status == SubscriptionStatus.CANCELED


@pytest.mark.asyncio
async def test_paid_plan_syncs_device_limit_to_remnawave(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=4003,
            username="device_limit",
            first_name="Device",
            last_name="Limit",
            language_code="ru",
        )
        await hub.accounts.adjust_balance(
            user_id=user.id,
            amount_rub=Decimal("500"),
            transaction_type=BalanceTransactionType.TOPUP,
            description="Seed balance",
        )
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED_WEEKLY, charge_user=True)
        remote_user = await hub.remnawave.get_user(subscription.user.remnawave_user_uuid)

    assert remote_user.hwidDeviceLimit == 8
