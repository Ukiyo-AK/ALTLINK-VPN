from __future__ import annotations

from decimal import Decimal

import pytest

from altlink.domain.enums import PlanCode


@pytest.mark.asyncio
async def test_server_capacity_is_not_auto_overwritten_by_current_clients(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3001,
            username="capacitycheck",
            first_name="Capacity",
            last_name="Check",
            language_code="ru",
        )
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=False)
        refreshed = await hub.accounts.get_user_by_telegram_id(3001)

        await hub.catalog.set_server_capacity(refreshed.assigned_server_id, 0)
        server = await hub.catalog.get_server(refreshed.assigned_server_id)

        assert server.current_clients == 1
        assert server.max_clients == 0
        assert Decimal(server.load_percent) == Decimal("0")


@pytest.mark.asyncio
async def test_server_load_uses_online_clients_instead_of_access_assignments(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3002,
            username="onlinecheck",
            first_name="Online",
            last_name="Check",
            language_code="ru",
        )
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=False)
        refreshed = await hub.accounts.get_user_by_telegram_id(3002)

        server = await hub.catalog.get_server(refreshed.assigned_server_id)
        server.users_online = 7
        await hub.catalog.set_server_capacity(server.id, 10)
        server = await hub.catalog.get_server(server.id)

        assert server.current_clients == 1
        assert server.users_online == 7
        assert Decimal(server.load_percent) == Decimal("70.00")
