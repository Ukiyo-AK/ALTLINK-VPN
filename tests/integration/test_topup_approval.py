from __future__ import annotations

from decimal import Decimal

import pytest


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
