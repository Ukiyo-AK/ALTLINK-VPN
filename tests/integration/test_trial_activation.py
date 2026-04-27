from __future__ import annotations

import pytest

from altlink.domain.enums import SubscriptionStatus, UserStatus


@pytest.mark.asyncio
async def test_trial_activation_creates_remote_user_subscription_and_server_assignment(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=2001,
            username="remoteuser",
            first_name="Remote",
            last_name="User",
            language_code="ru",
        )
        subscription = await hub.billing.activate_trial(user.id)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(2001)
        current = await hub.accounts.get_current_subscription(user.id)
        bundle = await hub.accounts.get_subscription_bundle(user.id)
        assert user.status == UserStatus.TRIAL
        assert user.remnawave_user_uuid is not None
        assert user.assigned_server is not None
        assert current.status == SubscriptionStatus.TRIAL
        assert subscription.id == current.id
        assert bundle["accessible_nodes"]


@pytest.mark.asyncio
async def test_trial_activation_uses_any_available_server_when_ten_gbit_unavailable(test_services):
    async with test_services.hub() as hub:
        for server in await hub.catalog.list_servers():
            if server.server_type.value == "ten_gbit":
                await hub.catalog.set_server_availability(server.id, False)

        user = await hub.accounts.get_or_create_user(
            telegram_id=2002,
            username="trialfallback",
            first_name="Trial",
            last_name="Fallback",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(2002)
        bundle = await hub.accounts.get_subscription_bundle(user.id)

        assert user.status == UserStatus.TRIAL
        assert user.assigned_server is not None
        assert user.assigned_server.server_type.value in {"whitelist", "regular"}
        assert {node.nodeName for node in bundle["accessible_nodes"]} == {"Whitelist EU", "Regular Warsaw"}
