from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from altlink.domain.enums import BalanceTransactionType
from altlink.domain.enums import PlanCode, SubscriptionStatus, UserStatus
from altlink.utils.time import utc_now


@pytest.mark.asyncio
async def test_daily_charge_moves_active_user_to_grace_then_blocked(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=4001,
            username="graceful",
            first_name="Grace",
            last_name="User",
            language_code="ru",
        )
        await hub.accounts.adjust_balance(
            user_id=user.id,
            amount_rub=Decimal("10"),
            transaction_type=BalanceTransactionType.TOPUP,
            description="Seed balance",
        )
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED, charge_user=True)
        subscription.next_billing_at = utc_now() - timedelta(minutes=1)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(4001)
        user.balance_rub = Decimal("0")
        await hub.billing.process_due_subscriptions()

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(4001)
        subscription = await hub.accounts.get_current_subscription(user.id)
        assert user.status == UserStatus.GRACE
        assert subscription.status == SubscriptionStatus.GRACE
        subscription.grace_until = utc_now() - timedelta(minutes=1)
        await hub.billing.process_due_subscriptions()

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(4001)
        latest = await hub.accounts.get_latest_subscription(user.id)
        assert user.status == UserStatus.BLOCKED
        assert latest.status == SubscriptionStatus.BLOCKED
