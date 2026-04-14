from __future__ import annotations

import pytest

from altlink.domain.enums import SubscriptionStatus, UserStatus


@pytest.mark.asyncio
async def test_trial_activation_creates_remote_user_and_subscription(test_services):
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
        assert user.status == UserStatus.TRIAL
        assert user.remnawave_user_uuid is not None
        assert current.status == SubscriptionStatus.TRIAL
        assert subscription.id == current.id

