from __future__ import annotations

from decimal import Decimal

import pytest


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
