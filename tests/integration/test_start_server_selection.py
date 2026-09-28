from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import select

from altlink.application.services.base import ConflictError, NotFoundError
from altlink.domain.enums import PlanCode, ServerType
from altlink.infrastructure.db.models import UserStartServer
from altlink.presentation.bots import admin_handlers
from altlink.presentation.bots.admin_keyboards import compact_callback_uuid
from altlink.utils.time import utc_now


async def setup_user(hub, services, telegram_id=96100):
    user = await hub.accounts.get_or_create_user(
        telegram_id=telegram_id, username=f"multi_{telegram_id}",
        first_name="Multi", last_name="Start", language_code="ru",
    )
    await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=False)
    first = await hub.catalog.get_server(user.assigned_server_id)
    node = services.remnawave._build_node(str(uuid4()), "Additional Start", "FI")
    services.remnawave.nodes[node.uuid] = node
    await hub.catalog.sync_servers()
    second = next(server for server in await hub.catalog.list_servers() if server.remnawave_node_uuid == node.uuid)
    await hub.catalog.set_server_type(second.id, ServerType.TEN_GBIT)
    return user, first, second


async def assigned_ids(hub, user_id):
    return {server.id for server in await hub.catalog.get_assigned_start_servers(user_id)}


async def active_start_ids(hub, user_id):
    return {access.server_id for access in await hub.catalog.get_user_servers(user_id)
            if access.server.server_type == ServerType.TEN_GBIT}


@pytest.mark.asyncio
async def test_multiple_start_servers_survive_sync_switch_and_renewal(test_services, monkeypatch):
    async with test_services.hub() as hub:
        user, first, second = await setup_user(hub, test_services)
        other = await hub.accounts.get_or_create_user(
            telegram_id=96101, username="other_multi", first_name="Other", last_name="User", language_code="ru",
        )
        await hub.billing.activate_paid_plan(other.id, PlanCode.SINGLE_10GBIT, charge_user=False)
        update = test_services.remnawave.update_user
        updates = []

        async def capture(payload):
            updates.append(payload["telegramId"])
            return await update(payload)

        monkeypatch.setattr(test_services.remnawave, "update_user", capture)
        await hub.catalog.set_start_servers(user.id, [first.id, second.id, second.id])
        assert updates == [user.telegram_id]
        user_id, expected_ids = user.id, {first.id, second.id}
        remote = test_services.remnawave.users[user.remnawave_user_uuid]
        assert {first.remnawave_internal_squad_uuid, second.remnawave_internal_squad_uuid} <= {
            squad.uuid for squad in remote.activeInternalSquads
        }

    async with test_services.hub() as hub:
        assert await assigned_ids(hub, user_id) == expected_ids
        await hub.catalog.rebuild_user_access_matrix()
        assert await active_start_ids(hub, user_id) == expected_ids
        await hub.billing.activate_paid_plan(user_id, PlanCode.UNLIMITED, charge_user=False)
        await hub.billing.activate_paid_plan(user_id, PlanCode.SINGLE_10GBIT_WEEKLY, charge_user=False)
        assert await active_start_ids(hub, user_id) == expected_ids
        user = await hub.accounts.get_user(user_id)
        user.balance_rub = Decimal("500")
        subscription = await hub.accounts.get_current_subscription(user_id)
        subscription.next_billing_at = utc_now() - timedelta(minutes=1)
        subscription.ends_at = subscription.next_billing_at
        await hub.session.flush()
        await hub.billing.process_due_subscriptions()
        assert await active_start_ids(hub, user_id) == expected_ids
        assert await assigned_ids(hub, user_id) == expected_ids
        assert user.balance_rub < Decimal("500")


@pytest.mark.asyncio
async def test_toggle_and_failed_remote_removal_preserve_assignments(test_services, monkeypatch):
    async with test_services.hub() as hub:
        user, first, second = await setup_user(hub, test_services)
        await hub.catalog.set_start_servers(user.id, [second.id], toggle=True)
        assert await assigned_ids(hub, user.id) == {first.id, second.id}
        original_update = test_services.remnawave.update_user

        async def ignore_update(payload):
            return test_services.remnawave.users[payload["uuid"]]

        monkeypatch.setattr(test_services.remnawave, "update_user", ignore_update)
        with pytest.raises(ConflictError, match="не подтвердила"):
            await hub.catalog.set_start_servers(user.id, [second.id])
        assert await assigned_ids(hub, user.id) == {first.id, second.id}
        assert await active_start_ids(hub, user.id) == {first.id, second.id}
        monkeypatch.setattr(test_services.remnawave, "update_user", original_update)
        await hub.catalog.set_start_servers(user.id, [first.id], toggle=True)
        assert await assigned_ids(hub, user.id) == {second.id}
        remote = test_services.remnawave.users[user.remnawave_user_uuid]
        assert first.remnawave_internal_squad_uuid not in {squad.uuid for squad in remote.activeInternalSquads}
        with pytest.raises(ConflictError, match="хотя бы один"):
            await hub.catalog.set_start_servers(user.id, [second.id], toggle=True)
        with pytest.raises(ConflictError, match="хотя бы один"):
            await hub.catalog.set_start_servers(user.id, [])
        with pytest.raises(NotFoundError):
            await hub.catalog.set_start_servers(user.id, [str(uuid4())])
        regular = next(server for server in await hub.catalog.list_servers() if server.server_type == ServerType.REGULAR)
        with pytest.raises(ConflictError, match="только сервер типа Start"):
            await hub.catalog.set_start_servers(user.id, [regular.id])
        assert await assigned_ids(hub, user.id) == {second.id}


@pytest.mark.asyncio
async def test_offline_and_whitelist_blocks_do_not_erase_start_selection(test_services):
    async with test_services.hub() as hub:
        user, first, second = await setup_user(hub, test_services)
        expected_ids = {first.id, second.id}
        await hub.catalog.set_start_servers(user.id, list(expected_ids))
        await hub.catalog.set_server_availability(first.id, False)
        assert await active_start_ids(hub, user.id) == {second.id}
        assert await assigned_ids(hub, user.id) == expected_ids
        assert (await hub.dashboard.summary())["affected_start_users_count"] == 0
        await hub.catalog.set_server_availability(second.id, False)
        assert (await hub.dashboard.summary())["affected_start_users_count"] == 1
        await hub.catalog.set_server_availability(first.id, True)
        await hub.catalog.set_server_availability(second.id, True)
        subscription = await hub.accounts.get_current_subscription(user.id)
        subscription.whitelist_included_limit_bytes = 0
        user.whitelist_extra_traffic_bytes = 0
        user.balance_rub = Decimal("-100")
        await hub.catalog.rebuild_user_access_matrix(user_id=user.id)
        assert await active_start_ids(hub, user.id) == expected_ids
        assert all(access.server.server_type != ServerType.WHITELIST
                   for access in await hub.catalog.get_user_servers(user.id))
        await hub.catalog.force_delete_server(first.id)
        assert await assigned_ids(hub, user.id) == {second.id}
        assert await active_start_ids(hub, user.id) == {second.id}


@pytest.mark.asyncio
async def test_new_offline_selection_is_rejected(test_services):
    async with test_services.hub() as hub:
        user, first, second = await setup_user(hub, test_services)
        await hub.catalog.set_server_availability(second.id, False)
        with pytest.raises(ConflictError, match="сейчас недоступен"):
            await hub.catalog.set_start_servers(user.id, [first.id, second.id])
        assert await assigned_ids(hub, user.id) == {first.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_table_exists", [True, False])
async def test_migration_backfills_legacy_start_without_granting_extra_servers(test_services, runtime_table_exists):
    async with test_services.hub() as hub:
        user, first, second = await setup_user(hub, test_services)
        await hub.session.flush()
        connection = await hub.session.connection()
        module = runpy.run_path(str(Path(__file__).resolve().parents[2] /
                                   "alembic/versions/20260922_0020_start_server_selection.py"))

        def migrate(bind):
            UserStartServer.__table__.drop(bind)
            if runtime_table_exists:
                UserStartServer.__table__.create(bind)
            with Operations.context(MigrationContext.configure(bind)):
                module["upgrade"]()
                module["upgrade"]()

        await connection.run_sync(migrate)
        rows = (await hub.session.execute(select(UserStartServer.user_id, UserStartServer.server_id))).all()
        assert rows == [(user.id, first.id)]
        assert user.assigned_server_id == first.id


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [True, False])
async def test_bot_multiple_selection_and_early_ack(test_services, monkeypatch, authorized):
    async with test_services.hub() as hub:
        user, first, second = await setup_user(hub, test_services)
        user_id, first_id, second_id = user.id, first.id, second.id
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=12345), answer=AsyncMock(),
        data=(f"{admin_handlers.USER_START_SERVER_ASSIGN_PREFIX}:0:"
              f"{compact_callback_uuid(second_id)}:"
              f"{compact_callback_uuid(user_id)}"),
    )
    monkeypatch.setattr(admin_handlers, "is_admin", AsyncMock(return_value=authorized))
    render = AsyncMock()
    monkeypatch.setattr(admin_handlers, "render_admin", render)
    original_update = test_services.remnawave.update_user

    async def update(payload):
        callback.answer.assert_awaited_once()
        return await original_update(payload)

    monkeypatch.setattr(test_services.remnawave, "update_user", update)
    await admin_handlers.assign_user_start_server(callback, test_services)
    async with test_services.hub() as hub:
        assert await assigned_ids(hub, user_id) == ({first_id, second_id} if authorized else {first_id})
    if authorized:
        callback.answer.assert_awaited_once()
        assert render.await_args.kwargs["answer_callback"] is False
        buttons = [button for row in render.await_args.kwargs["reply_markup"].inline_keyboard for button in row]
        assert sum(button.text.startswith("✓ ") for button in buttons) == 2
    else:
        callback.answer.assert_not_awaited()
        render.assert_not_awaited()
