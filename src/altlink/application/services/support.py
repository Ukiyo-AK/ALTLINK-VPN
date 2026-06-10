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
    ) -> SupportRequest:
        normalized = message.strip()
        if not normalized:
            raise ConflictError(
                "Опишите проблему текстом, чтобы мы могли передать её в поддержку."
            )

        normalized_topic = (topic or "vpn_issue").strip()[:64] or "vpn_issue"
        request = SupportRequest(user_id=user_id, topic=normalized_topic, message=normalized)
        self.session.add(request)
        await self.session.flush()
        self.session.add(
            SupportMessage(
                support_request_id=request.id,
                user_id=user_id,
                sender_type="user",
                message=normalized,
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
        item = await self.session.get(
            SupportRequest,
            request_id,
            options=[
                joinedload(SupportRequest.user),
                joinedload(SupportRequest.resolved_by_admin),
                selectinload(SupportRequest.messages).joinedload(SupportMessage.admin),
            ],
        )
        if item is None:
            raise NotFoundError("Запрос поддержки не найден.")
        return item

    async def add_user_message(self, request_id: str, *, user_id: str, message: str) -> SupportMessage:
        item = await self.get_request(request_id)
        if item.user_id != user_id:
            raise NotFoundError("Запрос поддержки не найден.")
        if item.status == SupportRequestStatus.RESOLVED:
            raise ConflictError("Этот запрос уже закрыт. Создайте новый, если нужна помощь.")
        normalized = message.strip()
        if not normalized:
            raise ConflictError("Напишите сообщение для поддержки.")
        support_message = SupportMessage(
            support_request_id=item.id,
            user_id=user_id,
            sender_type="user",
            message=normalized,
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
