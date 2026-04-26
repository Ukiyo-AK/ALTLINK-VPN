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
