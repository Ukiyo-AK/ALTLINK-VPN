from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from altlink.domain.enums import BalanceTransactionType, PlanCode
from altlink.infrastructure.db.models import BalanceTransaction
from altlink.utils.time import utc_now


@pytest.mark.asyncio
async def test_stub_topup_adds_money_and_notification(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3001,
            username="paying",
            first_name="Pay",
            last_name="User",
            language_code="ru",
        )
        request = await hub.topups.create_request(user.id, Decimal("350"), auto_complete=True)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(3001)
        requests = await hub.topups.list_requests(user_id=user.id)
        assert Decimal(user.balance_rub) == Decimal("350")
        assert request.id == requests[0].id
        assert requests[0].status == "approved"
        pending = await hub.notifications.pending_query()
        queued = list((await hub.session.scalars(pending)).all())
        assert any(item.user_id == user.id for item in queued)


@pytest.mark.asyncio
async def test_manual_topup_stays_pending_until_admin_approval(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3002,
            username="manual_pay",
            first_name="Manual",
            last_name="User",
            language_code="ru",
        )
        request = await hub.topups.create_request(user.id, Decimal("275"), auto_complete=False)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(3002)
        requests = await hub.topups.list_requests(user_id=user.id)
        assert Decimal(user.balance_rub) == Decimal("0")
        assert requests[0].id == request.id
        assert str(requests[0].status) == "new"

        approved = await hub.topups.approve(request.id, admin_id=None, comment="manual approve")
        user = await hub.accounts.get_user_by_telegram_id(3002)
        assert str(approved.status) == "approved"
        assert Decimal(user.balance_rub) == Decimal("275")


@pytest.mark.asyncio
async def test_topup_checkout_stays_manual_by_default(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3003,
            username="manual_checkout",
            first_name="Manual",
            last_name="Checkout",
            language_code="ru",
        )
        checkout = await hub.topups.create_checkout(user.id, Decimal("150"))

    async with test_services.hub() as hub:
        refreshed = await hub.accounts.get_user_by_telegram_id(3003)
        request = await hub.topups.get_request(checkout.request.id)
        assert hub.topups.resolved_provider() == "manual"
        assert checkout.provider == "manual"
        assert checkout.admin_required is True
        assert checkout.auto_completed is False
        assert str(request.status) == "new"
        assert Decimal(refreshed.balance_rub) == Decimal("0")


@pytest.mark.asyncio
async def test_wata_without_api_falls_back_to_stub_checkout(test_services):
    test_services.settings.payment_provider = "wata"
    test_services.settings.wata_api_token = ""

    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3004,
            username="wata_stub",
            first_name="Wata",
            last_name="Stub",
            language_code="ru",
        )
        checkout = await hub.topups.create_checkout(user.id, Decimal("125"))

    async with test_services.hub() as hub:
        refreshed = await hub.accounts.get_user_by_telegram_id(3004)
        request = await hub.topups.get_request(checkout.request.id)
        assert hub.topups.resolved_provider() == "stub"
        assert checkout.provider == "stub"
        assert checkout.auto_completed is True
        assert str(request.status) == "approved"
        assert Decimal(refreshed.balance_rub) == Decimal("125")


@pytest.mark.asyncio
async def test_plan_switch_keeps_compensation_message_only_in_transaction_history(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3005,
            username="carryover_once",
            first_name="Carry",
            last_name="Over",
            language_code="ru",
        )
        await hub.accounts.adjust_balance(
            user_id=user.id,
            amount_rub=Decimal("500"),
            transaction_type=BalanceTransactionType.TOPUP,
            description="Seed balance",
        )
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        current = await hub.accounts.get_current_subscription(user.id)
        assert current is not None
        current.ends_at = utc_now() + timedelta(days=15)
        current.next_billing_at = current.ends_at

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(3005)
        switched = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED, charge_user=True)
        transactions = list(
            (
                await hub.session.scalars(
                    select(BalanceTransaction)
                    .where(BalanceTransaction.user_id == user.id)
                    .order_by(BalanceTransaction.created_at.asc())
                )
            ).all()
        )

    compensation_descriptions = [
        item.description
        for item in transactions
        if item.type == BalanceTransactionType.REFUND
        and item.description == "Компенсация остатка прошлого тарифа при смене плана"
    ]
    assert compensation_descriptions == ["Компенсация остатка прошлого тарифа при смене плана"]
    assert switched.notes is None or "Компенсация остатка прошлого тарифа" not in switched.notes
