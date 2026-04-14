from __future__ import annotations

import pytest

from altlink.application.services.base import ConflictError


@pytest.mark.asyncio
async def test_trial_can_be_activated_only_once(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=1001,
            username="trialuser",
            first_name="Trial",
            last_name="User",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(1001)
        with pytest.raises(ConflictError):
            await hub.billing.activate_trial(user.id)

