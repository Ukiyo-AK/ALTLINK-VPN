from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

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
async def test_sync_servers_keeps_new_node_when_internal_squad_api_fails(test_services, monkeypatch):
    new_node = test_services.remnawave._build_node("new-node-with-squad-api-down", "Fresh Frankfurt", "DE")
    test_services.remnawave.nodes[new_node.uuid] = new_node

    async def broken_internal_squads():
        raise httpx.ConnectError("internal squads endpoint unavailable")

    monkeypatch.setattr(test_services.remnawave, "list_internal_squads", broken_internal_squads)

    async with test_services.hub() as hub:
        servers = await hub.catalog.sync_servers()

    assert any(server.remnawave_node_uuid == new_node.uuid for server in servers)

    async with test_services.hub() as hub:
        refreshed_servers = await hub.catalog.list_servers()

    assert any(server.remnawave_node_uuid == new_node.uuid for server in refreshed_servers)


@pytest.mark.asyncio
async def test_sync_servers_deduplicates_duplicate_active_inbound_tags(test_services):
    new_node = test_services.remnawave._build_node(
        "new-node-with-duplicate-inbound-tags",
        "Fresh Duplicate Inbounds",
        "DE",
    )
    first_inbound = new_node.configProfile.activeInbounds[0]
    duplicate_inbound = first_inbound.model_copy(
        update={
            "uuid": str(uuid4()),
            "port": 8443,
        }
    )
    new_node.configProfile.activeInbounds.append(duplicate_inbound)
    test_services.remnawave.nodes[new_node.uuid] = new_node

    async with test_services.hub() as hub:
        servers = await hub.catalog.sync_servers()
        created = next(server for server in servers if server.remnawave_node_uuid == new_node.uuid)
        matching_inbounds = [inbound for inbound in created.inbounds if inbound.tag == first_inbound.tag]

    assert len(matching_inbounds) == 1
    assert matching_inbounds[0].remnawave_inbound_uuid == duplicate_inbound.uuid
    assert matching_inbounds[0].port == 8443
    assert matching_inbounds[0].access_type == "regular"


@pytest.mark.asyncio
async def test_sync_servers_keeps_new_node_when_remote_user_squad_sync_fails(test_services, monkeypatch):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3006,
            username="freshnodeuser",
            first_name="Fresh",
            last_name="Node",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)

    new_node = test_services.remnawave._build_node("new-node-with-user-sync-down", "Fresh Prague", "CZ")
    test_services.remnawave.nodes[new_node.uuid] = new_node

    async def broken_update_user(payload: dict):
        raise ValueError("unexpected remote payload error")

    monkeypatch.setattr(test_services.remnawave, "update_user", broken_update_user)

    async with test_services.hub() as hub:
        servers = await hub.catalog.sync_servers()

    assert any(server.remnawave_node_uuid == new_node.uuid for server in servers)

    async with test_services.hub() as hub:
        refreshed_servers = await hub.catalog.list_servers()

    assert any(server.remnawave_node_uuid == new_node.uuid for server in refreshed_servers)


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
async def test_start_user_keeps_pinned_server_until_admin_reassigns_it(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3008,
            username="startfailover",
            first_name="Start",
            last_name="Failover",
            language_code="ru",
        )
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=False)
        user = await hub.accounts.get_user_by_telegram_id(3008)
        previous_server = await hub.catalog.get_server(user.assigned_server_id)

        backup_node = test_services.remnawave._build_node(
            str(uuid4()),
            "Reserve 10G",
            "DE",
        )
        test_services.remnawave.nodes[backup_node.uuid] = backup_node
        await hub.catalog.sync_servers()
        backup_server = next(
            server
            for server in await hub.catalog.list_servers()
            if server.remnawave_node_uuid == backup_node.uuid
        )
        await hub.catalog.set_server_type(backup_server.id, ServerType.TEN_GBIT)

        test_services.remnawave.nodes[previous_server.remnawave_node_uuid] = (
            test_services.remnawave.nodes[previous_server.remnawave_node_uuid].model_copy(
                update={"isConnected": True, "isDisabled": True}
            )
        )
        summary = await hub.catalog.refresh_server_health_and_failover()

        refreshed_user = await hub.accounts.get_user(user.id)
        active_accesses = await hub.catalog.get_user_servers(user.id)
        remote_user = test_services.remnawave.users[user.remnawave_user_uuid]
        remote_squad_ids = {squad.uuid for squad in remote_user.activeInternalSquads}

        assert refreshed_user.assigned_server_id == previous_server.id
        assert previous_server.id not in {access.server_id for access in active_accesses}
        assert backup_server.id not in {access.server_id for access in active_accesses}
        assert backup_server.remnawave_internal_squad_uuid not in remote_squad_ids
        assert previous_server.remnawave_internal_squad_uuid not in remote_squad_ids
        assert summary["start_failovers"] == []
        assert summary["affected_start_users"] == [
            {
                "user_id": user.id,
                "telegram_id": user.telegram_id,
                "server_id": previous_server.id,
            }
        ]
        preferred_while_unavailable = await hub.catalog.assign_preferred_server(
            user.id,
            PlanCode.SINGLE_10GBIT,
        )
        assert preferred_while_unavailable.id == previous_server.id
        assert refreshed_user.assigned_server_id == previous_server.id

        test_services.remnawave.nodes[previous_server.remnawave_node_uuid] = (
            test_services.remnawave.nodes[previous_server.remnawave_node_uuid].model_copy(
                update={"isConnected": True, "isDisabled": False}
            )
        )
        await hub.catalog.refresh_server_health_and_failover()
        refreshed_user = await hub.accounts.get_user(user.id)
        active_accesses = await hub.catalog.get_user_servers(user.id)
        remote_user = test_services.remnawave.users[user.remnawave_user_uuid]

        assert refreshed_user.assigned_server_id == previous_server.id
        assert previous_server.id in {access.server_id for access in active_accesses}
        assert previous_server.remnawave_internal_squad_uuid in {
            squad.uuid for squad in remote_user.activeInternalSquads
        }


@pytest.mark.asyncio
async def test_admin_can_manually_reassign_start_user_and_invalid_targets_are_rejected(
    test_services,
    monkeypatch,
):
    async with test_services.hub() as hub:
        start_user = await hub.accounts.get_or_create_user(
            telegram_id=3009,
            username="manualstartserver",
            first_name="Manual",
            last_name="Start",
            language_code="ru",
        )
        await hub.billing.activate_paid_plan(
            start_user.id,
            PlanCode.SINGLE_10GBIT,
            charge_user=False,
        )
        start_user = await hub.accounts.get_user(start_user.id)
        previous_server_id = start_user.assigned_server_id

        backup_node = test_services.remnawave._build_node(
            str(uuid4()),
            "Manual Reserve 10G",
            "FI",
        )
        test_services.remnawave.nodes[backup_node.uuid] = backup_node
        await hub.catalog.sync_servers()
        backup_server = next(
            server
            for server in await hub.catalog.list_servers()
            if server.remnawave_node_uuid == backup_node.uuid
        )
        await hub.catalog.set_server_type(backup_server.id, ServerType.TEN_GBIT)

        reassigned = await hub.catalog.reassign_start_server(
            start_user.id,
            backup_server.id,
            admin_id=None,
        )
        start_user = await hub.accounts.get_user(start_user.id)
        remote_user = test_services.remnawave.users[start_user.remnawave_user_uuid]
        remote_squad_ids = {squad.uuid for squad in remote_user.activeInternalSquads}
        events = await hub.accounts.list_user_events(start_user.id, limit=10)

        assert reassigned.id == backup_server.id
        assert start_user.assigned_server_id == backup_server.id
        assert backup_server.remnawave_internal_squad_uuid in remote_squad_ids
        assert any(
            event.event_type == "start_server_manually_reassigned"
            and event.payload["from_server_id"] == previous_server_id
            and event.payload["to_server_id"] == backup_server.id
            for event in events
        )
        preferred = await hub.catalog.assign_preferred_server(
            start_user.id,
            PlanCode.SINGLE_10GBIT_WEEKLY,
        )
        assert preferred.id == backup_server.id
        assert start_user.assigned_server_id == backup_server.id

        original_update_user = test_services.remnawave.update_user

        async def unavailable_update_user(payload: dict):
            if payload.get("telegramId") == start_user.telegram_id:
                raise httpx.ConnectError("temporary remote sync failure")
            return await original_update_user(payload)

        monkeypatch.setattr(test_services.remnawave, "update_user", unavailable_update_user)
        with pytest.raises(ConflictError, match="не подтвердила назначение"):
            await hub.catalog.reassign_start_server(start_user.id, previous_server_id)
        start_user = await hub.accounts.get_user(start_user.id)
        remote_user = test_services.remnawave.users[start_user.remnawave_user_uuid]

        assert start_user.assigned_server_id == backup_server.id
        assert backup_server.remnawave_internal_squad_uuid in {
            squad.uuid for squad in remote_user.activeInternalSquads
        }
        monkeypatch.setattr(test_services.remnawave, "update_user", original_update_user)

        pro_user = await hub.accounts.get_or_create_user(
            telegram_id=3010,
            username="manualproserver",
            first_name="Manual",
            last_name="Pro",
            language_code="ru",
        )
        await hub.billing.activate_paid_plan(
            pro_user.id,
            PlanCode.UNLIMITED,
            charge_user=False,
        )
        with pytest.raises(ConflictError, match="только пользователям тарифа Start"):
            await hub.catalog.reassign_start_server(pro_user.id, backup_server.id)

        await hub.catalog.set_server_availability(backup_server.id, False)
        with pytest.raises(ConflictError, match="сейчас недоступен"):
            await hub.catalog.reassign_start_server(start_user.id, backup_server.id)


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
