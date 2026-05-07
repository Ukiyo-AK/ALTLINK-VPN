from __future__ import annotations

from decimal import Decimal

import pytest

from altlink.domain.enums import AccessStatus, PlanCode, ServerType


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


@pytest.mark.asyncio
async def test_server_type_change_does_not_require_internal_squad_api(test_services, monkeypatch):
    async def fail_internal_squads(*args, **kwargs):
        raise AssertionError("internal squad API should not be called when only the server type changes")

    monkeypatch.setattr(test_services.remnawave, "list_internal_squads", fail_internal_squads)
    monkeypatch.setattr(test_services.remnawave, "create_internal_squad", fail_internal_squads)
    monkeypatch.setattr(test_services.remnawave, "update_internal_squad", fail_internal_squads)

    async with test_services.hub() as hub:
        server = (await hub.catalog.list_servers())[0]
        target_type = ServerType.WHITELIST if server.server_type != ServerType.WHITELIST else ServerType.REGULAR

        updated = await hub.catalog.set_server_type(server.id, target_type)
        refreshed = await hub.catalog.get_server(server.id)

        assert updated.server_type == target_type
        assert refreshed.server_type == target_type


@pytest.mark.asyncio
async def test_sync_does_not_recreate_squad_for_missing_node(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3003,
            username="missingnode",
            first_name="Missing",
            last_name="Node",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)
        user = await hub.accounts.get_user_by_telegram_id(3003)

        removed = next(server for server in await hub.catalog.list_servers() if "Regular" in server.name)
        removed_squad_uuid = removed.remnawave_internal_squad_uuid
        assert removed_squad_uuid
        assert removed_squad_uuid in test_services.remnawave.internal_squads

        test_services.remnawave.nodes.pop(removed.remnawave_node_uuid)
        test_services.remnawave.internal_squads.pop(removed_squad_uuid)

        await hub.catalog.sync_servers()

        refreshed = await hub.catalog.get_server(removed.id)
        active_accesses = await hub.catalog.get_user_servers(user.id)
        all_accesses = await hub.catalog.get_user_servers(user.id, active_only=False)
        remote_user = test_services.remnawave.users[user.remnawave_user_uuid]
        remote_squad_ids = {item.uuid for item in remote_user.activeInternalSquads}

        assert not refreshed.is_connected
        assert refreshed.users_online == 0
        assert all(not inbound.is_active for inbound in refreshed.inbounds)
        assert removed_squad_uuid not in test_services.remnawave.internal_squads
        assert refreshed.id not in {access.server_id for access in active_accesses}
        assert next(access for access in all_accesses if access.server_id == refreshed.id).status == AccessStatus.BLOCKED
        assert removed_squad_uuid not in remote_squad_ids
