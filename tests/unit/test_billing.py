from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from altlink.application.services.billing import BillingService
from altlink.application.services.base import ConflictError
from altlink.domain.billing import compute_period_end, compute_prorated_daily_charge, quantize_money
from altlink.domain.enums import BalanceTransactionType, PlanCode, SubscriptionStatus
from altlink.domain.plans import SINGLE_10GBIT_MONTHLY_PRICE_RUB
from altlink.infrastructure.db.models import BalanceTransaction
from altlink.infrastructure.remnawave_schemas import RemoteSeriesPoint, RemoteUsageResponse, RemoteUsageTopNode
from altlink.utils.time import utc_now


def test_compute_period_end_uses_fixed_day_window():
    started_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert compute_period_end(started_at, 30) == datetime(2026, 1, 31, 12, 0, tzinfo=UTC)


def test_prorated_daily_charges_sum_to_monthly_price():
    charges = [compute_prorated_daily_charge(Decimal("200"), 30, day) for day in range(1, 31)]
    assert quantize_money(sum(charges, Decimal("0"))) == Decimal("200.00")
    assert charges[0] == Decimal("6.67")


def test_low_balance_reminder_window_matches_expected_checkpoints():
    assert BillingService._low_balance_reminder_window(timedelta(days=2)) == ("3d", "меньше 3 дней")
    assert BillingService._low_balance_reminder_window(timedelta(hours=12)) == ("1d", "меньше 1 дня")
    assert BillingService._low_balance_reminder_window(timedelta(minutes=30)) == ("1h", "меньше 1 часа")
    assert BillingService._low_balance_reminder_window(timedelta(days=5)) is None
    assert BillingService._low_balance_reminder_window(timedelta(0)) is None


@pytest.mark.asyncio
async def test_snapshot_traffic_uses_node_usage_for_whitelist_tracking_when_user_top_nodes_miss_it(
    test_services,
    monkeypatch,
):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13001,
            username="whitelist_metered",
            first_name="White",
            last_name="List",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        user = await hub.accounts.get_user(user.id)

        whitelist_node = next(node for node in test_services.remnawave.nodes.values() if "Whitelist" in node.name)
        test_services.remnawave.set_usage(
            user.remnawave_user_uuid,
            used_bytes=12 * 1024**3,
            lifetime_used_bytes=12 * 1024**3,
        )
        test_services.remnawave.set_node_usage(
            whitelist_node.uuid,
            user.remnawave_user_uuid,
            3 * 1024**3,
        )

        async def fake_get_user_usage(user_uuid, start, end):
            return RemoteUsageResponse(
                categories=[start.isoformat(), end.isoformat()],
                sparklineData=[0, 0],
                topNodes=[
                    RemoteUsageTopNode(
                        uuid="non-whitelist-node",
                        color="#00aaff",
                        name="Regular Node",
                        countryCode="DE",
                        total=9 * 1024**3,
                    )
                ],
                series=[
                    RemoteSeriesPoint(
                        uuid="non-whitelist-node",
                        name="Regular Node",
                        color="#00aaff",
                        countryCode="DE",
                        total=9 * 1024**3,
                        data=[9 * 1024**3, 0],
                    )
                ],
            )

        monkeypatch.setattr(test_services.remnawave, "get_user_usage", fake_get_user_usage)

        await hub.billing.snapshot_traffic()

        refreshed = await hub.accounts.get_current_subscription(user.id)

    assert refreshed is not None
    assert refreshed.traffic_used_bytes == 12 * 1024**3
    assert refreshed.whitelist_traffic_used_bytes == 3 * 1024**3


@pytest.mark.asyncio
async def test_start_whitelist_traffic_is_charged_immediately_and_caps_balance_at_minus_fifty(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13021,
            username="start_instant_whitelist",
            first_name="Start",
            last_name="Instant",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("150"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        whitelist_node = next(node for node in test_services.remnawave.nodes.values() if "Whitelist" in node.name)

        test_services.remnawave.set_usage(
            user.remnawave_user_uuid,
            used_bytes=40 * 1024**3,
            lifetime_used_bytes=40 * 1024**3,
        )
        test_services.remnawave.set_node_usage(
            whitelist_node.uuid,
            user.remnawave_user_uuid,
            40 * 1024**3,
        )

        await hub.billing.snapshot_traffic()

        refreshed_user = await hub.accounts.get_user(user.id)
        refreshed_subscription = await hub.accounts.get_current_subscription(user.id)

    assert refreshed_subscription is not None
    assert Decimal(refreshed_user.balance_rub) == Decimal("-50.00")
    assert refreshed_subscription.whitelist_traffic_used_bytes == 40 * 1024**3
    assert 0 < refreshed_subscription.whitelist_traffic_billed_bytes < refreshed_subscription.whitelist_traffic_used_bytes


@pytest.mark.asyncio
async def test_start_renewal_charge_does_not_repeat_whitelist_usage_that_was_already_charged(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13022,
            username="start_renewal_charge",
            first_name="Start",
            last_name="Renewal",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        whitelist_node = next(node for node in test_services.remnawave.nodes.values() if "Whitelist" in node.name)

        test_services.remnawave.set_usage(
            user.remnawave_user_uuid,
            used_bytes=2 * 1024**3,
            lifetime_used_bytes=2 * 1024**3,
        )
        test_services.remnawave.set_node_usage(
            whitelist_node.uuid,
            user.remnawave_user_uuid,
            2 * 1024**3,
        )

        await hub.billing.snapshot_traffic()
        refreshed_user = await hub.accounts.get_user(user.id)
        refreshed_subscription = await hub.accounts.get_current_subscription(user.id)

        renewal_charge = hub.billing._compute_renewal_charge(refreshed_subscription, refreshed_subscription.plan)

    assert refreshed_subscription is not None
    assert Decimal(refreshed_user.balance_rub) == Decimal("23.00")
    assert renewal_charge == SINGLE_10GBIT_MONTHLY_PRICE_RUB


@pytest.mark.asyncio
async def test_failed_plan_switch_does_not_credit_balance_or_cancel_current_subscription(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13023,
            username="failed_switch_credit",
            first_name="Failed",
            last_name="Switch",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        current = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        current.ends_at = utc_now() + timedelta(days=15)
        current.next_billing_at = current.ends_at
        current.started_at = utc_now() - timedelta(days=15)
        user.balance_rub = Decimal("-10.00")
        balance_before = Decimal(user.balance_rub)

        with pytest.raises(ConflictError) as exc_info:
            await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED, charge_user=True)

        refreshed_user = await hub.accounts.get_user(user.id)
        refreshed_current = await hub.accounts.get_current_subscription(user.id)
        refunds = list(
            (
                await hub.session.scalars(
                    select(BalanceTransaction).where(
                        BalanceTransaction.user_id == user.id,
                        BalanceTransaction.type == BalanceTransactionType.REFUND,
                    )
                )
            ).all()
        )

    assert "Недостаточно средств" in str(exc_info.value)
    assert Decimal(refreshed_user.balance_rub) == balance_before
    assert refreshed_current is not None
    assert refreshed_current.id == current.id
    assert refreshed_current.status == SubscriptionStatus.ACTIVE
    assert refunds == []
