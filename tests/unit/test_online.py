from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from altlink.domain.enums import NotificationType, PlanCode
from altlink.infrastructure.db.models import Notification


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


@pytest.mark.asyncio
async def test_online_refresh_queues_whitelist_notice_for_start_plan_once(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13002,
            username="whitelist_notice",
            first_name="White",
            last_name="Notice",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        subscription.whitelist_traffic_used_bytes = 2 * 1024**3
        subscription.whitelist_traffic_billed_bytes = 2 * 1024**3

        remote = await hub.remnawave.get_user(user.remnawave_user_uuid)
        whitelist_node = next(node for node in hub.remnawave.nodes.values() if "Whitelist" in node.name)
        remote.userTraffic.lastConnectedNodeUuid = whitelist_node.uuid

        await hub.online.refresh_online_cache(detailed=False)
        await hub.online.refresh_online_cache(detailed=False)

        notifications = list(
            (
                await hub.session.scalars(
                    select(Notification).where(Notification.type == NotificationType.BROADCAST)
                )
            ).all()
        )

    assert len(notifications) == 1
    assert notifications[0].type == NotificationType.BROADCAST
    assert "ТРАФИК ПО БЕЛЫМ СПИСКАМ СПИСЫВАЕТСЯ С БАЛАНСА СРАЗУ" in notifications[0].message
