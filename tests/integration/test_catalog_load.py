from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from altlink.application.services.base import ConflictError, NotFoundError
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

        active_accesses = await hub.catalog.get_user_servers(user.id)
        all_accesses = await hub.catalog.get_user_servers(user.id, active_only=False)
        servers = await hub.catalog.list_servers()
        remote_user = test_services.remnawave.users[user.remnawave_user_uuid]
        remote_squad_ids = {item.uuid for item in remote_user.activeInternalSquads}

        assert removed_squad_uuid not in test_services.remnawave.internal_squads
        assert removed.id not in {server.id for server in servers}
        assert removed.id not in {access.server_id for access in active_accesses}
        assert removed.id not in {access.server_id for access in all_accesses}
        assert removed_squad_uuid not in remote_squad_ids
        with pytest.raises(NotFoundError):
            await hub.catalog.get_server(removed.id)


@pytest.mark.asyncio
async def test_strict_squad_sync_error_identifies_problem_server(test_services, monkeypatch):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3031,
            username="squadduplicate",
            first_name="Squad",
            last_name="Duplicate",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)
        user = await hub.accounts.get_user_by_telegram_id(3031)
        target_servers = [access.server for access in await hub.catalog.get_user_servers(user.id) if access.server]
        expected_squad_names = [hub.catalog._squad_name(server) for server in target_servers]

        async def empty_squads():
            return []

        async def duplicate_squad(*, name: str, inbounds: list[str]):
            request = httpx.Request("POST", "https://remna.example/api/internal-squads")
            response = httpx.Response(409, request=request, json={"message": "Internal squad name already exists"})
            raise httpx.HTTPStatusError("duplicate", request=request, response=response)

        monkeypatch.setattr(test_services.remnawave, "list_internal_squads", empty_squads)
        monkeypatch.setattr(test_services.remnawave, "create_internal_squad", duplicate_squad)

        with pytest.raises(ConflictError) as exc_info:
            await hub.catalog.sync_user_target_squads(user.id)

    message = str(exc_info.value)

    assert "Internal squad name already exists" in message
    assert any(f"Сервер: {server.name}" in message for server in target_servers)
    assert any(f"локальный server_id: {server.id}" in message for server in target_servers)
    assert any(f"node_uuid: {server.remnawave_node_uuid}" in message for server in target_servers)
    assert any(f"squad_name: {squad_name}" in message for squad_name in expected_squad_names)


@pytest.mark.asyncio
async def test_sync_survives_missing_ten_gbit_server_for_existing_start_user(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3004,
            username="startmissingnode",
            first_name="Start",
            last_name="Missing",
            language_code="ru",
        )
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=False)
        user = await hub.accounts.get_user_by_telegram_id(3004)
        assigned_server_id = user.assigned_server_id
        assigned = await hub.catalog.get_server(assigned_server_id)

        assert assigned.server_type == ServerType.TEN_GBIT

        test_services.remnawave.nodes.pop(assigned.remnawave_node_uuid)

        await hub.catalog.sync_servers()

        refreshed_user = await hub.accounts.get_user(user.id)
        active_accesses = await hub.catalog.get_user_servers(user.id)
        all_accesses = await hub.catalog.get_user_servers(user.id, active_only=False)

        assert refreshed_user.assigned_server_id is None
        assert assigned_server_id not in {access.server_id for access in all_accesses}
        assert all(access.server.server_type != ServerType.TEN_GBIT for access in active_accesses if access.server)
        assert any(access.server.server_type == ServerType.WHITELIST for access in active_accesses if access.server)


@pytest.mark.asyncio
async def test_force_delete_server_removes_local_server_and_accesses(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3005,
            username="forcedeleteserver",
            first_name="Force",
            last_name="Delete",
            language_code="ru",
        )
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=False)
        user = await hub.accounts.get_user_by_telegram_id(3005)
        server = await hub.catalog.get_server(user.assigned_server_id)
        squad_uuid = server.remnawave_internal_squad_uuid

        summary = await hub.catalog.force_delete_server(server.id)

        servers = await hub.catalog.list_servers()
        active_accesses = await hub.catalog.get_user_servers(user.id)
        all_accesses = await hub.catalog.get_user_servers(user.id, active_only=False)
        user = await hub.accounts.get_user_by_telegram_id(3005)
        remote_user = test_services.remnawave.users[user.remnawave_user_uuid]

        assert summary["server_id"] == server.id
        assert summary["assigned_users"] == 1
        assert summary["accesses"] >= 1
        assert summary["inbounds"] >= 1
        assert server.id not in {item.id for item in servers}
        assert user.assigned_server_id != server.id
        assert server.id not in {access.server_id for access in active_accesses}
        assert server.id not in {access.server_id for access in all_accesses}
        assert squad_uuid not in {item.uuid for item in remote_user.activeInternalSquads}
        assert squad_uuid in test_services.remnawave.internal_squads
