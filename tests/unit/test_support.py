from __future__ import annotations

import pytest

from altlink.application.services.base import ConflictError
from altlink.domain.enums import SupportRequestStatus


@pytest.mark.asyncio
async def test_support_request_can_be_created_and_resolved(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14001,
            username="support_user",
            first_name="Support",
            last_name="User",
            language_code="ru",
        )
        item = await hub.support.create_request(
            user_id=user.id,
            message="VPN не подключается на iPhone после обновления приложения.",
        )
        assert item.status == SupportRequestStatus.NEW

        admin = await hub.accounts.create_admin(
            username="support_admin",
            password="password",
            full_name="Support Admin",
            telegram_id=555001,
        )

    async with test_services.hub() as hub:
        items = await hub.support.list_requests(status=SupportRequestStatus.NEW)
        assert items[0].message.startswith("VPN не подключается")

        resolved = await hub.support.resolve_request(
            item.id,
            admin_id=admin.id,
            resolution_comment="Запрос обработан",
        )
        assert resolved.status == SupportRequestStatus.RESOLVED
        assert resolved.resolution_comment == "Запрос обработан"


@pytest.mark.asyncio
async def test_support_request_rejects_empty_message(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14002,
            username="empty_support",
            first_name="Empty",
            last_name="Support",
            language_code="ru",
        )
        with pytest.raises(ConflictError):
            await hub.support.create_request(user_id=user.id, message="   ")
