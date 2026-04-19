from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from altlink.application.services.accounts import AccountService
from altlink.application.services.base import BaseService, ConflictError, NotFoundError
from altlink.application.services.notifications import NotificationService
from altlink.domain.enums import BalanceTransactionType, NotificationType, SystemEventLevel, TopupStatus
from altlink.domain.notifications import topup_approved_message, topup_rejected_message
from altlink.infrastructure.db.models import TopupRequest
from altlink.utils.time import utc_now


class TopupService(BaseService):
    source = "topups"

    def __init__(
        self,
        *,
        session,
        settings,
        remnawave,
        accounts: AccountService,
        notifications: NotificationService,
    ) -> None:
        super().__init__(session, settings, remnawave)
        self.accounts = accounts
        self.notifications = notifications

    async def create_request(
        self,
        user_id: str,
        amount_rub: Decimal,
        comment: str | None = None,
        *,
        auto_complete: bool = True,
    ) -> TopupRequest:
        if amount_rub <= 0:
            raise ConflictError("Сумма пополнения должна быть больше нуля.")
        request = TopupRequest(user_id=user_id, amount_rub=Decimal(amount_rub), user_comment=comment)
        self.session.add(request)
        await self.session.flush()
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="topup_created",
            message="Создан новый платёж на пополнение.",
            payload={"topup_request_id": request.id, "user_id": user_id, "amount": str(amount_rub)},
        )
        if auto_complete:
            await self.approve(request.id, admin_id=None, comment="auto_stub")
        return request

    async def list_requests(
        self, *, status: TopupStatus | None = None, user_id: str | None = None
    ) -> list[TopupRequest]:
        query = select(TopupRequest).options(joinedload(TopupRequest.user)).order_by(TopupRequest.created_at.desc())
        if status is not None:
            query = query.where(TopupRequest.status == status)
        if user_id is not None:
            query = query.where(TopupRequest.user_id == user_id)
        return list((await self.session.scalars(query.limit(200))).all())

    async def get_request(self, request_id: str) -> TopupRequest:
        item = await self.session.get(TopupRequest, request_id, options=[joinedload(TopupRequest.user)])
        if item is None:
            raise NotFoundError("Платёж не найден.")
        return item

    async def approve(self, request_id: str, admin_id: str | None, comment: str | None = None) -> TopupRequest:
        item = await self.get_request(request_id)
        if item.status != TopupStatus.NEW:
            raise ConflictError("Подтвердить можно только новый платёж.")
        item.status = TopupStatus.APPROVED
        item.admin_comment = comment
        item.approved_by_admin_id = admin_id
        item.approved_at = utc_now()

        await self.accounts.adjust_balance(
            user_id=item.user_id,
            amount_rub=Decimal(item.amount_rub),
            transaction_type=BalanceTransactionType.TOPUP,
            description=f"Пополнение баланса #{item.id}",
            admin_id=admin_id,
            topup_request_id=item.id,
        )
        await self.notifications.queue(
            user_id=item.user_id,
            notification_type=NotificationType.TOPUP_APPROVED,
            message=topup_approved_message(Decimal(item.amount_rub)),
            dedupe_key=f"topup-approved:{item.id}",
        )
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="topup_approved",
            message="Платёж на пополнение успешно подтверждён.",
            payload={"topup_request_id": item.id},
            actor_admin_id=admin_id,
        )
        return item

    async def reject(self, request_id: str, admin_id: str | None, comment: str | None = None) -> TopupRequest:
        item = await self.get_request(request_id)
        if item.status != TopupStatus.NEW:
            raise ConflictError("Отклонить можно только новый платёж.")
        item.status = TopupStatus.REJECTED
        item.admin_comment = comment
        item.approved_by_admin_id = admin_id
        item.rejected_at = utc_now()
        await self.notifications.queue(
            user_id=item.user_id,
            notification_type=NotificationType.TOPUP_REJECTED,
            message=topup_rejected_message(Decimal(item.amount_rub), comment),
            dedupe_key=f"topup-rejected:{item.id}",
        )
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="topup_rejected",
            message="Платёж на пополнение отклонён.",
            payload={"topup_request_id": item.id},
            actor_admin_id=admin_id,
        )
        return item

    async def cancel(self, request_id: str, user_id: str) -> TopupRequest:
        item = await self.get_request(request_id)
        if item.user_id != user_id:
            raise ConflictError("Можно отменить только свой платёж.")
        if item.status != TopupStatus.NEW:
            raise ConflictError("Отменить можно только новый платёж.")
        item.status = TopupStatus.CANCELED
        item.canceled_at = utc_now()
        return item
