from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.asyncio
async def test_online_refresh_links_remote_user_by_telegram_id(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13001,
            username="online_user",
            first_name="Online",
            last_name="User",
            language_code="ru",
        )
        remote = await hub.remnawave.create_user(
            {
                "username": "remote_online_user",
                "telegramId": user.telegram_id,
                "expireAt": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                "status": "ACTIVE",
                "trafficLimitBytes": 0,
            }
        )
        remote.userTraffic.lastConnectedNodeUuid = next(iter(hub.remnawave.nodes))

        created = await hub.online.refresh_online_cache(detailed=True)
        updated = await hub.accounts.get_user(user.id)

        assert any(item.user_id == user.id for item in created)
        assert updated.remnawave_user_uuid == remote.uuid
        assert updated.remnawave_username == remote.username
