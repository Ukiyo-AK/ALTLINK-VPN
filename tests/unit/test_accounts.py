from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from altlink.application.services.accounts import UserListFilters
from altlink.application.services.base import ConflictError
from altlink.domain.enums import PlanCode
from altlink.infrastructure.db.models import OnlineSessionCache, TrafficSnapshot


@pytest.mark.asyncio
async def test_list_users_supports_username_and_remote_identifiers(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11001,
            username="demo_user",
            first_name="Demo",
            last_name="User",
            language_code="ru",
        )
        user.remnawave_user_uuid = "remote-user-uuid"
        user.remnawave_short_uuid = "short-uuid"
        user.remnawave_username = "remote_demo"
        user_id = user.id

    async with test_services.hub() as hub:
        assert (await hub.accounts.list_users("@demo_user"))[0].id == user_id
        assert (await hub.accounts.list_users("11001"))[0].id == user_id
        assert (await hub.accounts.list_users("remote_demo"))[0].id == user_id
        assert (await hub.accounts.list_users("remote-user-uuid"))[0].id == user_id
        assert (await hub.accounts.list_users("short-uuid"))[0].id == user_id


@pytest.mark.asyncio
async def test_list_users_for_admin_filters_and_sorts_across_all_users(test_services):
    async with test_services.hub() as hub:
        target = await hub.accounts.get_or_create_user(
            telegram_id=11030,
            username="filtered_target",
            first_name="Filtered",
            last_name="Target",
            language_code="ru",
        )
        other = await hub.accounts.get_or_create_user(
            telegram_id=11031,
            username="filtered_other",
            first_name="Filtered",
            last_name="Other",
            language_code="ru",
        )
        await hub.topups.create_request(target.id, Decimal("500"), auto_complete=True)
        await hub.topups.create_request(other.id, Decimal("500"), auto_complete=True)
        await hub.billing.activate_paid_plan(target.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        await hub.billing.activate_paid_plan(other.id, PlanCode.UNLIMITED, charge_user=True)

        target = await hub.accounts.get_user(target.id)
        subscription = await hub.accounts.get_current_subscription(target.id)
        server_id = target.assigned_server_id
        subscription.whitelist_traffic_used_bytes = 3 * 1024**3
        hub.session.add(
            TrafficSnapshot(
                user_id=target.id,
                subscription_id=subscription.id,
                server_id=server_id,
                snapshot_date=date.today(),
                used_bytes=8 * 1024**3,
                lifetime_used_bytes=8 * 1024**3,
                source="test",
            )
        )
        hub.session.add_all(
            [
                OnlineSessionCache(user_id=target.id, server_id=server_id, device="iPhone", is_online=True),
                OnlineSessionCache(user_id=target.id, server_id=server_id, device="Windows", is_online=True),
                OnlineSessionCache(user_id=other.id, device="Android", is_online=True),
            ]
        )
        await hub.session.flush()

        page = await hub.accounts.list_users_for_admin(
            UserListFilters(
                status="active",
                plan="start",
                balance_min=Decimal("100"),
                traffic_min_bytes=5 * 1024**3,
                whitelist_traffic_min_bytes=2 * 1024**3,
                node_id=server_id,
                node_traffic_min_bytes=5 * 1024**3,
                device_count_min=2,
                sort="traffic",
                direction="desc",
                limit=5,
            )
        )

    assert page.total == 1
    assert page.limit == 5
    assert [item.id for item in page.users] == [target.id]
    assert page.users[0].admin_total_traffic_bytes == 8 * 1024**3
    assert page.users[0].admin_whitelist_traffic_bytes == 3 * 1024**3
    assert page.users[0].admin_node_traffic_bytes == 8 * 1024**3
    assert page.users[0].admin_device_count == 2


@pytest.mark.asyncio
async def test_complete_registration_marks_user_as_registered(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11002,
            username="registration_user",
            first_name="Registration",
            last_name="User",
            language_code="ru",
        )
        assert not hub.accounts.is_registered(user)

        await hub.accounts.complete_registration(user.id)
        updated = await hub.accounts.get_user(user.id)

        assert hub.accounts.is_registered(updated)
        assert updated.registration_completed_at is not None
        assert updated.consent_accepted_at is not None
        assert updated.consent_version == hub.accounts.registration_consent_version


@pytest.mark.asyncio
async def test_mark_channel_verified_persists_timestamp(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11003,
            username="channel_user",
            first_name="Channel",
            last_name="User",
            language_code="ru",
        )
        assert user.channel_verified_at is None

        await hub.accounts.mark_channel_verified(user.id)
        updated = await hub.accounts.get_user(user.id)

        assert updated.channel_verified_at is not None


@pytest.mark.asyncio
async def test_mark_promo_onboarding_completed_persists_timestamp(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11006,
            username="promo_onboarding_user",
            first_name="Promo",
            last_name="Onboarding",
            language_code="ru",
        )
        assert user.promo_onboarding_completed_at is None

        await hub.accounts.mark_promo_onboarding_completed(user.id)
        updated = await hub.accounts.get_user(user.id)

        assert updated.promo_onboarding_completed_at is not None


@pytest.mark.asyncio
async def test_is_trial_available_without_trial_period_returns_true(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11004,
            username="trial_free_user",
            first_name="Trial",
            last_name="Free",
            language_code="ru",
        )

        assert await hub.accounts.is_trial_available(user.id) is True


@pytest.mark.asyncio
async def test_can_offer_trial_requires_clean_account_history(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11005,
            username="clean_user",
            first_name="Clean",
            last_name="User",
            language_code="ru",
        )

        assert await hub.accounts.can_offer_trial(user.id) is True

        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)

        assert await hub.accounts.can_offer_trial(user.id) is False


@pytest.mark.asyncio
async def test_get_subscription_bundle_tolerates_missing_subscription_info(test_services, monkeypatch):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11007,
            username="bundle_partial_user",
            first_name="Bundle",
            last_name="Partial",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)
        short_uuid = user.remnawave_short_uuid

    async def missing_subscription_info(short_uuid_value: str):
        request = httpx.Request("GET", f"https://remna.example/api/sub/{short_uuid_value}/info")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr(test_services.remnawave, "get_subscription_info", missing_subscription_info)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(11007)
        bundle = await hub.accounts.get_subscription_bundle(user.id)

    assert bundle["subscription"] is not None
    assert bundle["accessible_nodes"]
    assert bundle["connection_keys"] is not None
    assert bundle["subscription_info"] is None
    assert bundle["subscription_url"] == f"https://remna.example/api/sub/{short_uuid}"
    assert user.remnawave_short_uuid == short_uuid


@pytest.mark.asyncio
async def test_list_admin_telegram_ids_merges_settings_and_admin_rows(test_services):
    test_services.settings.admin_allowed_telegram_ids = [101, 202]

    async with test_services.hub() as hub:
        await hub.accounts.create_admin(
            username="panel_admin",
            password="secret123",
            full_name="Panel Admin",
            telegram_id=303,
        )
        ids = await hub.accounts.list_admin_telegram_ids()

    assert ids == [101, 202, 303]


@pytest.mark.asyncio
async def test_hwid_devices_are_loaded_and_deleted_for_current_user_only(test_services):
    async with test_services.hub() as hub:
        first = await hub.accounts.get_or_create_user(
            telegram_id=11008,
            username="first_device_user",
            first_name="First",
            last_name="Device",
            language_code="ru",
        )
        second = await hub.accounts.get_or_create_user(
            telegram_id=11009,
            username="second_device_user",
            first_name="Second",
            last_name="Device",
            language_code="ru",
        )
        await hub.billing.activate_trial(first.id)
        await hub.billing.activate_trial(second.id)
        own_device = test_services.remnawave.add_hwid_device(first.remnawave_user_uuid, hwid="first-hwid")
        foreign_device = test_services.remnawave.add_hwid_device(second.remnawave_user_uuid, hwid="second-hwid")

        devices = await hub.accounts.list_user_hwid_devices(first.id)
        await hub.accounts.delete_user_hwid_device(first.id, foreign_device.hwid)
        untouched_foreign_devices = await hub.accounts.list_user_hwid_devices(second.id)
        await hub.accounts.delete_user_hwid_device(first.id, own_device.hwid)
        remaining_devices = await hub.accounts.list_user_hwid_devices(first.id)

    assert [item.hwid for item in devices] == ["first-hwid"]
    assert [item.hwid for item in untouched_foreign_devices] == ["second-hwid"]
    assert remaining_devices == []


@pytest.mark.asyncio
async def test_revoke_user_subscription_link_updates_local_short_uuid(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11010,
            username="revoke_link_user",
            first_name="Revoke",
            last_name="Link",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)
        old_short_uuid = user.remnawave_short_uuid

        remote = await hub.accounts.revoke_user_subscription_link(user.id)
        updated = await hub.accounts.get_user(user.id)

    assert remote.shortUuid != old_short_uuid
    assert updated.remnawave_short_uuid == remote.shortUuid


@pytest.mark.asyncio
async def test_vless_keys_download_filters_keys_and_enforces_cooldown(test_services):
    original_get_connection_keys = test_services.remnawave.get_connection_keys
    get_connection_keys = AsyncMock(side_effect=original_get_connection_keys)
    test_services.remnawave.get_connection_keys = get_connection_keys

    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11011,
            username="vless_keys_user",
            first_name="Vless",
            last_name="Keys",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)

        keys = await hub.accounts.get_rate_limited_user_vless_keys(user.id)
        with pytest.raises(ConflictError, match="5 мин"):
            await hub.accounts.get_rate_limited_user_vless_keys(user.id)

    assert len(keys) == 2
    assert all(item.startswith("vless://") for item in keys)
    assert get_connection_keys.await_count == 1
