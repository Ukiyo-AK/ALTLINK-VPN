from __future__ import annotations

from decimal import Decimal

import pytest

from altlink.domain.enums import PlanCode


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
