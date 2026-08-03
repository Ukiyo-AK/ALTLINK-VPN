from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from altlink.domain.enums import BalanceTransactionType, PlanCode
from altlink.utils.time import utc_now


@pytest.mark.asyncio
async def test_dashboard_top_users_supports_balance_and_topups(test_services):
    async with test_services.hub() as hub:
        user_one = await hub.accounts.get_or_create_user(
            telegram_id=12001,
            username="leader",
            first_name="Top",
            last_name="One",
            language_code="ru",
        )
        user_two = await hub.accounts.get_or_create_user(
            telegram_id=12002,
            username="runner",
            first_name="Top",
            last_name="Two",
            language_code="ru",
        )

        await hub.topups.create_request(user_one.id, Decimal("500"), auto_complete=True)
        await hub.topups.create_request(user_two.id, Decimal("150"), auto_complete=True)

        by_balance = await hub.dashboard.top_users("balance")
        by_topups = await hub.dashboard.top_users("topups")

        assert by_balance[0].user.id == user_one.id
        assert Decimal(by_balance[0].value) == Decimal("500.00")
        assert by_topups[0].user.id == user_one.id
        assert Decimal(by_topups[0].value) == Decimal("500.00")


@pytest.mark.asyncio
async def test_dashboard_top_users_traffic_uses_latest_lifetime_snapshot(test_services):
    async with test_services.hub() as hub:
        user_one = await hub.accounts.get_or_create_user(
            telegram_id=12003,
            username="traffic_leader",
            first_name="Traffic",
            last_name="Leader",
            language_code="ru",
        )
        user_two = await hub.accounts.get_or_create_user(
            telegram_id=12004,
            username="traffic_runner",
            first_name="Traffic",
            last_name="Runner",
            language_code="ru",
        )
        await hub.topups.create_request(user_one.id, Decimal("500"), auto_complete=True)
        await hub.topups.create_request(user_two.id, Decimal("500"), auto_complete=True)
        await hub.billing.activate_paid_plan(user_one.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        await hub.billing.activate_paid_plan(user_two.id, PlanCode.SINGLE_10GBIT, charge_user=True)

        user_one = await hub.accounts.get_user(user_one.id)
        user_two = await hub.accounts.get_user(user_two.id)
        test_services.remnawave.set_usage(
            user_one.remnawave_user_uuid,
            used_bytes=0,
            lifetime_used_bytes=22 * 1024**3,
        )
        test_services.remnawave.set_usage(
            user_two.remnawave_user_uuid,
            used_bytes=3 * 1024**3,
            lifetime_used_bytes=9 * 1024**3,
        )
        await hub.billing.snapshot_traffic()

        by_traffic = await hub.dashboard.top_users("traffic")
        overview = await hub.dashboard.overview()

    assert by_traffic[0].user.id == user_one.id
    assert int(by_traffic[0].value) == 22 * 1024**3
    assert overview["top_users"][0].user.id == user_one.id
    assert overview["top_users"][0].traffic_used_bytes == 22 * 1024**3


@pytest.mark.asyncio
async def test_dashboard_overview_contains_start_pro_plan_mix(test_services):
    async with test_services.hub() as hub:
        start_user = await hub.accounts.get_or_create_user(
            telegram_id=12005,
            username="start_ratio",
            first_name="Start",
            last_name="Ratio",
            language_code="ru",
        )
        pro_user = await hub.accounts.get_or_create_user(
            telegram_id=12006,
            username="pro_ratio",
            first_name="Pro",
            last_name="Ratio",
            language_code="ru",
        )
        await hub.topups.create_request(start_user.id, Decimal("500"), auto_complete=True)
        await hub.topups.create_request(pro_user.id, Decimal("500"), auto_complete=True)
        await hub.billing.activate_paid_plan(start_user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        await hub.billing.activate_paid_plan(pro_user.id, PlanCode.UNLIMITED, charge_user=True)

        overview = await hub.dashboard.overview()

    assert overview["charts"]["plan_mix"]["labels"] == ["Start", "Pro"]
    assert overview["charts"]["plan_mix"]["values"] == [1, 1]


@pytest.mark.asyncio
async def test_dashboard_overview_contains_daily_user_snapshots_without_load_charts(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=12007,
            username="analytics_user",
            first_name="Analytics",
            last_name="User",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)

        trial_user = await hub.accounts.get_or_create_user(
            telegram_id=12010,
            username="analytics_trial_user",
            first_name="Analytics",
            last_name="Trial",
            language_code="ru",
        )
        await hub.billing.activate_trial(trial_user.id)

        overview = await hub.dashboard.overview(period="1d")

    charts = overview["charts"]

    assert overview["period"] == "1d"
    assert overview["period_label"] == "1 день"
    assert overview["new_users_in_period"] >= 2
    assert overview["new_paid_users_in_period"] >= 1
    assert Decimal(overview["payments_total_rub"]) >= Decimal("500")
    assert len(charts["users"]["labels"]) == 1
    assert charts["users"]["datasets"]["active_paid_users"][-1] >= 1
    assert charts["users"]["datasets"]["trial_users"][-1] >= 1
    assert sum(charts["users"]["datasets"]["new_users"]) >= 2
    assert sum(charts["users"]["datasets"]["new_paid_users"]) >= 1
    assert charts["plan_signups"]["datasets"]
    assert "server_loads" not in charts
    assert "host_loads" not in charts
    assert "traffic" in charts


@pytest.mark.asyncio
async def test_dashboard_conversion_funnel_tracks_trial_connection_and_paid_conversion(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=12011,
            username="conversion_user",
            first_name="Conversion",
            last_name="User",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)
        user = await hub.accounts.get_user(user.id)
        test_services.remnawave.set_usage(
            user.remnawave_user_uuid,
            used_bytes=64 * 1024**2,
            lifetime_used_bytes=64 * 1024**2,
        )
        await hub.billing.snapshot_traffic()
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED, charge_user=True)

        overview = await hub.dashboard.overview(period="1d")

    funnel = overview["charts"]["conversion_funnel"]
    assert funnel["counts"] == [1, 1, 1, 1]
    assert funnel["percentages"] == [100.0, 100.0, 100.0, 100.0]
    assert [stage["percent_from_previous"] for stage in funnel["stages"]] == [100.0, 100.0, 100.0, 100.0]


@pytest.mark.asyncio
async def test_dashboard_weekly_user_chart_uses_separate_daily_snapshots(test_services):
    async with test_services.hub() as hub:
        paid_user = await hub.accounts.get_or_create_user(
            telegram_id=12012,
            username="daily_paid_user",
            first_name="Daily",
            last_name="Paid",
            language_code="ru",
        )
        trial_user = await hub.accounts.get_or_create_user(
            telegram_id=12013,
            username="daily_trial_user",
            first_name="Daily",
            last_name="Trial",
            language_code="ru",
        )
        paid_subscription = await hub.billing.activate_paid_plan(
            paid_user.id,
            PlanCode.SINGLE_10GBIT,
            charge_user=False,
        )
        trial_subscription = await hub.billing.activate_trial(trial_user.id)

        now = utc_now()
        started_at = now - timedelta(days=3)
        for user in (paid_user, trial_user):
            user.created_at = started_at
        for subscription in (paid_subscription, trial_subscription):
            subscription.created_at = started_at
            subscription.started_at = started_at
            subscription.ends_at = now + timedelta(days=2)
        await hub.session.flush()

        overview = await hub.dashboard.overview(period="1w")

    users = overview["charts"]["users"]
    assert len(users["labels"]) == 7
    assert users["datasets"]["active_paid_users"][0] == 0
    assert users["datasets"]["trial_users"][0] == 0
    assert users["datasets"]["active_paid_users"][-1] == 1
    assert users["datasets"]["trial_users"][-1] == 1
    assert sum(users["datasets"]["new_users"]) == 2


@pytest.mark.asyncio
async def test_transaction_filters_apply_to_full_query(test_services):
    async with test_services.hub() as hub:
        matching = await hub.accounts.get_or_create_user(
            telegram_id=12008,
            username="transaction_filter_target",
            first_name="Filter",
            last_name="Target",
            language_code="ru",
        )
        other = await hub.accounts.get_or_create_user(
            telegram_id=12009,
            username="transaction_filter_other",
            first_name="Filter",
            last_name="Other",
            language_code="ru",
        )
        await hub.accounts.adjust_balance(
            user_id=matching.id,
            amount_rub=Decimal("75"),
            transaction_type=BalanceTransactionType.MANUAL_ADJUSTMENT,
            description="Искомая корректировка",
        )
        await hub.accounts.adjust_balance(
            user_id=other.id,
            amount_rub=Decimal("500"),
            transaction_type=BalanceTransactionType.TOPUP,
            description="Другая операция",
        )

        rows = await hub.dashboard.list_transactions(
            search="transaction_filter_target",
            transaction_type=BalanceTransactionType.MANUAL_ADJUSTMENT,
            amount_min=Decimal("70"),
            amount_max=Decimal("80"),
            limit=25,
        )

    assert [item.user_id for item in rows] == [matching.id]
