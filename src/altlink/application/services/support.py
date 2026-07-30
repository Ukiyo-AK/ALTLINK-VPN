from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from altlink.application.services.base import BaseService, ConflictError, NotFoundError
from altlink.domain.enums import SupportRequestStatus, SystemEventLevel
from altlink.infrastructure.db.models import SupportMessage, SupportRequest
from altlink.utils.time import utc_now


class SupportService(BaseService):
    source = "support"

    async def create_request(
        self,
        *,
        user_id: str,
        message: str,
        topic: str = "vpn_issue",
        attachment_path: str | None = None,
        attachment_mime_type: str | None = None,
        attachment_original_name: str | None = None,
        attachment_size: int | None = None,
    ) -> SupportRequest:
        normalized = message.strip()
        if not normalized and not attachment_path:
            raise ConflictError(
                "Опишите проблему или прикрепите фотографию, чтобы мы могли передать обращение в поддержку."
            )
        stored_message = normalized or "Прикреплена фотография."

        normalized_topic = (topic or "vpn_issue").strip()[:64] or "vpn_issue"
        request = SupportRequest(user_id=user_id, topic=normalized_topic, message=stored_message)
        self.session.add(request)
        await self.session.flush()
        self.session.add(
            SupportMessage(
                support_request_id=request.id,
                user_id=user_id,
                sender_type="user",
                message=stored_message,
                attachment_path=attachment_path,
                attachment_mime_type=attachment_mime_type,
                attachment_original_name=attachment_original_name,
                attachment_size=attachment_size,
            )
        )
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="support_request_created",
            message="Пользователь создал запрос в поддержку.",
            payload={"support_request_id": request.id, "user_id": user_id, "topic": normalized_topic},
        )
        await self.session.flush()
        return request

    async def list_requests(
        self,
        *,
        status: SupportRequestStatus | None = None,
        limit: int = 50,
    ) -> list[SupportRequest]:
        query = (
            select(SupportRequest)
            .options(
                joinedload(SupportRequest.user),
                joinedload(SupportRequest.resolved_by_admin),
                selectinload(SupportRequest.messages).joinedload(SupportMessage.admin),
            )
            .order_by(SupportRequest.created_at.desc())
            .limit(limit)
        )
        if status is not None:
            query = query.where(SupportRequest.status == status)
        return list((await self.session.scalars(query)).all())

    async def list_user_requests(self, user_id: str, *, limit: int = 20) -> list[SupportRequest]:
        query = (
            select(SupportRequest)
            .options(
                joinedload(SupportRequest.resolved_by_admin),
                selectinload(SupportRequest.messages).joinedload(SupportMessage.admin),
            )
            .where(SupportRequest.user_id == user_id)
            .order_by(SupportRequest.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(query)).all())

    async def get_request(self, request_id: str) -> SupportRequest:
        item = await self.session.scalar(
            select(SupportRequest)
            .options(
                joinedload(SupportRequest.user),
                joinedload(SupportRequest.resolved_by_admin),
                selectinload(SupportRequest.messages).joinedload(SupportMessage.admin),
            )
            .where(SupportRequest.id == request_id)
            .execution_options(populate_existing=True)
        )
        if item is None:
            raise NotFoundError("Запрос поддержки не найден.")
        return item

    async def add_user_message(
        self,
        request_id: str,
        *,
        user_id: str,
        message: str,
        attachment_path: str | None = None,
        attachment_mime_type: str | None = None,
        attachment_original_name: str | None = None,
        attachment_size: int | None = None,
    ) -> SupportMessage:
        item = await self.get_request(request_id)
        if item.user_id != user_id:
            raise NotFoundError("Запрос поддержки не найден.")
        if item.status == SupportRequestStatus.RESOLVED:
            raise ConflictError("Этот запрос уже закрыт. Создайте новый, если нужна помощь.")
        normalized = message.strip()
        if not normalized and not attachment_path:
            raise ConflictError("Напишите сообщение или прикрепите фотографию.")
        support_message = SupportMessage(
            support_request_id=item.id,
            user_id=user_id,
            sender_type="user",
            message=normalized or "Прикреплена фотография.",
            attachment_path=attachment_path,
            attachment_mime_type=attachment_mime_type,
            attachment_original_name=attachment_original_name,
            attachment_size=attachment_size,
        )
        self.session.add(support_message)
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="support_user_message_created",
            message="Пользователь добавил сообщение в чат поддержки.",
            payload={"support_request_id": item.id, "user_id": user_id},
        )
        await self.session.flush()
        return support_message

    async def add_admin_message(
        self,
        request_id: str,
        *,
        admin_id: str | None,
        message: str,
    ) -> SupportMessage:
        item = await self.get_request(request_id)
        if item.status == SupportRequestStatus.RESOLVED:
            raise ConflictError("Этот запрос уже закрыт.")
        normalized = message.strip()
        if not normalized:
            raise ConflictError("Напишите текст ответа.")
        support_message = SupportMessage(
            support_request_id=item.id,
            admin_id=admin_id,
            sender_type="admin",
            message=normalized,
        )
        self.session.add(support_message)
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="support_admin_message_created",
            message="Администратор ответил в чате поддержки.",
            payload={"support_request_id": item.id, "user_id": item.user_id},
            actor_admin_id=admin_id,
        )
        await self.session.flush()
        return support_message

    async def resolve_request(
        self,
        request_id: str,
        *,
        admin_id: str | None = None,
        resolution_comment: str | None = None,
    ) -> SupportRequest:
        item = await self.get_request(request_id)
        if item.status == SupportRequestStatus.RESOLVED:
            return item
        item.status = SupportRequestStatus.RESOLVED
        item.resolution_comment = resolution_comment
        item.resolved_by_admin_id = admin_id
        item.resolved_at = utc_now()
        normalized_comment = (resolution_comment or "").strip()
        if normalized_comment:
            self.session.add(
                SupportMessage(
                    support_request_id=item.id,
                    admin_id=admin_id,
                    sender_type="admin",
                    message=normalized_comment,
                )
            )
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="support_request_resolved",
            message="Запрос поддержки закрыт администратором.",
            payload={"support_request_id": item.id, "user_id": item.user_id},
            actor_admin_id=admin_id,
        )
        await self.session.flush()
        return item
