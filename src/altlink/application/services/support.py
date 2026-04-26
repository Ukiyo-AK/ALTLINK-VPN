from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from altlink.application.services.base import BaseService, ConflictError, NotFoundError
from altlink.domain.enums import SupportRequestStatus, SystemEventLevel
from altlink.infrastructure.db.models import SupportRequest
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
            raise ConflictError("Опишите проблему текстом, чтобы мы могли передать её в поддержку.")

        request = SupportRequest(user_id=user_id, topic=topic, message=normalized)
        self.session.add(request)
        await self.session.flush()
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="support_request_created",
            message="Пользователь создал запрос в поддержку.",
            payload={"support_request_id": request.id, "user_id": user_id, "topic": topic},
        )
        return request

    async def list_requests(
        self,
        *,
        status: SupportRequestStatus | None = None,
        limit: int = 50,
    ) -> list[SupportRequest]:
        query = (
            select(SupportRequest)
            .options(joinedload(SupportRequest.user), joinedload(SupportRequest.resolved_by_admin))
            .order_by(SupportRequest.created_at.desc())
            .limit(limit)
        )
        if status is not None:
            query = query.where(SupportRequest.status == status)
        return list((await self.session.scalars(query)).all())

    async def get_request(self, request_id: str) -> SupportRequest:
        item = await self.session.get(
            SupportRequest,
            request_id,
            options=[joinedload(SupportRequest.user), joinedload(SupportRequest.resolved_by_admin)],
        )
        if item is None:
            raise NotFoundError("Запрос поддержки не найден.")
        return item

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
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="support_request_resolved",
            message="Запрос поддержки закрыт администратором.",
            payload={"support_request_id": item.id, "user_id": item.user_id},
            actor_admin_id=admin_id,
        )
        return item
