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


@pytest.mark.asyncio
async def test_support_request_accepts_photo_without_text(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14003,
            username="photo_support",
            first_name="Photo",
            last_name="Support",
            language_code="ru",
        )
        item = await hub.support.create_request(
            user_id=user.id,
            message="",
            attachment_path="safe-photo.jpg",
            attachment_mime_type="image/jpeg",
            attachment_original_name="screen.jpg",
            attachment_size=128,
        )

        assert item.message == "Прикреплена фотография."
        loaded = await hub.support.get_request(item.id)
        assert loaded.messages[0].attachment_path == "safe-photo.jpg"
        assert loaded.messages[0].attachment_mime_type == "image/jpeg"


@pytest.mark.asyncio
async def test_support_user_can_reply_with_photo_only(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14004,
            username="photo_reply",
            first_name="Photo",
            last_name="Reply",
            language_code="ru",
        )
        item = await hub.support.create_request(user_id=user.id, message="Нужна помощь.")
        reply = await hub.support.add_user_message(
            item.id,
            user_id=user.id,
            message="",
            attachment_path="reply.png",
            attachment_mime_type="image/png",
            attachment_original_name="reply.png",
            attachment_size=256,
        )

        assert reply.message == "Прикреплена фотография."
        assert reply.attachment_path == "reply.png"
