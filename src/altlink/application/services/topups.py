from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from altlink.application.services.accounts import AccountService
from altlink.application.services.base import BaseService, ConflictError, NotFoundError
from altlink.application.services.notifications import NotificationService
from altlink.domain.enums import BalanceTransactionType, NotificationType, SystemEventLevel, TopupStatus
from altlink.domain.notifications import topup_approved_message, topup_rejected_message
from altlink.infrastructure.db.models import TopupRequest
from altlink.utils.time import utc_now


@dataclass(slots=True)
class TopupCheckoutSession:
    request: TopupRequest
    provider: str
    payment_url: str | None = None
    admin_required: bool = False
    auto_completed: bool = False


@dataclass(slots=True)
class TopupStatusSnapshot:
    request: TopupRequest
    provider: str
    payment_url: str | None
    external_status: str
    is_paid: bool
    is_final: bool
    is_stub: bool = False


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

    def resolved_provider(self) -> str:
        configured = (self.settings.payment_provider or "manual").strip().lower()
        if configured == "wata":
            return "wata" if self._is_wata_configured() else "stub"
        if configured == "stub":
            return "stub"
        return "manual"

    async def create_checkout(
        self,
        user_id: str,
        amount_rub: Decimal,
        comment: str | None = None,
    ) -> TopupCheckoutSession:
        provider = self.resolved_provider()
        if provider == "wata":
            request = await self.create_request(user_id, amount_rub, comment=comment, auto_complete=False)
            payment_url = await self._create_wata_payment_link(request)
            return TopupCheckoutSession(
                request=request,
                provider=provider,
                payment_url=payment_url,
                admin_required=False,
                auto_completed=False,
            )
        if provider == "manual":
            request = await self.create_request(user_id, amount_rub, comment=comment, auto_complete=False)
            return TopupCheckoutSession(
                request=request,
                provider=provider,
                payment_url=None,
                admin_required=True,
                auto_completed=False,
            )

        request = await self.create_request(user_id, amount_rub, comment=comment, auto_complete=True)
        return TopupCheckoutSession(
            request=request,
            provider="stub",
            payment_url=None,
            admin_required=False,
            auto_completed=True,
        )

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

    async def check_checkout_status(self, request_id: str) -> TopupStatusSnapshot:
        request = await self.get_request(request_id)
        provider = self.resolved_provider()

        if request.status == TopupStatus.APPROVED:
            return TopupStatusSnapshot(
                request=request,
                provider=provider,
                payment_url=None,
                external_status="approved",
                is_paid=True,
                is_final=True,
                is_stub=provider == "stub",
            )
        if request.status in {TopupStatus.REJECTED, TopupStatus.CANCELED}:
            return TopupStatusSnapshot(
                request=request,
                provider=provider,
                payment_url=None,
                external_status=str(request.status),
                is_paid=False,
                is_final=True,
                is_stub=False,
            )
        if provider != "wata":
            return TopupStatusSnapshot(
                request=request,
                provider=provider,
                payment_url=None,
                external_status="stub" if provider == "stub" else "manual",
                is_paid=provider == "stub",
                is_final=provider == "stub",
                is_stub=provider == "stub",
            )

        transaction = await self._fetch_wata_transaction(request.id)
        payment_url = await self._fetch_wata_payment_link(request.id)
        external_status = self._normalize_wata_status(transaction.get("transactionStatus"))

        if external_status == "paid":
            if request.status == TopupStatus.NEW:
                transaction_id = transaction.get("transactionId")
                comment = f"wata:{transaction_id}" if transaction_id else "wata"
                request = await self.approve(request.id, admin_id=None, comment=comment)
            return TopupStatusSnapshot(
                request=request,
                provider=provider,
                payment_url=payment_url,
                external_status=external_status,
                is_paid=True,
                is_final=True,
            )

        if external_status in {"declined", "expired"} and request.status == TopupStatus.NEW:
            request = await self.reject(request.id, admin_id=None, comment=f"wata:{external_status}")

        return TopupStatusSnapshot(
            request=request,
            provider=provider,
            payment_url=payment_url,
            external_status=external_status,
            is_paid=False,
            is_final=external_status in {"declined", "expired"},
        )

    def _is_wata_configured(self) -> bool:
        return bool((self.settings.wata_api_base_url or "").strip() and (self.settings.wata_api_token or "").strip())

    async def _create_wata_payment_link(self, request: TopupRequest) -> str:
        payload = {
            "type": "OneTime",
            "amount": float(Decimal(request.amount_rub)),
            "currency": self.settings.default_currency,
            "orderId": request.id,
            "description": f"ALTLINK balance top-up #{request.id}",
        }
        if self.settings.wata_success_redirect_url:
            payload["successRedirectUrl"] = self.settings.wata_success_redirect_url
        if self.settings.wata_fail_redirect_url:
            payload["failRedirectUrl"] = self.settings.wata_fail_redirect_url

        async with self._wata_client() as client:
            response = await client.post("/links", json=payload)
            response.raise_for_status()
            data = response.json()
        url = (data or {}).get("url")
        if not url:
            raise ConflictError("WATA не вернула ссылку на оплату.")
        return str(url)

    async def _fetch_wata_transaction(self, order_id: str) -> dict:
        async with self._wata_client() as client:
            response = await client.get("/transactions", params={"orderId": order_id, "maxResultCount": 10})
            response.raise_for_status()
            data = response.json() or {}
        items = data.get("items") or []
        if not items:
            return {}
        items.sort(
            key=lambda item: (
                self._wata_status_priority(self._normalize_wata_status(item.get("transactionStatus"))),
                item.get("createdAt") or "",
            )
        )
        return items[0]

    async def _fetch_wata_payment_link(self, order_id: str) -> str | None:
        async with self._wata_client() as client:
            response = await client.get("/links", params={"orderId": order_id, "maxResultCount": 10})
            response.raise_for_status()
            data = response.json() or {}
        items = data.get("items") or []
        if not items:
            return None
        latest = items[0]
        url = latest.get("url")
        return str(url) if url else None

    def _normalize_wata_status(self, raw_status: object) -> str:
        normalized = str(raw_status or "").strip().lower()
        if normalized in {"paid", "success", "succeeded"}:
            return "paid"
        if normalized in {"declined", "failed", "cancelled", "canceled"}:
            return "declined"
        if normalized in {"expired"}:
            return "expired"
        if normalized in {"created", "pending", "processing"}:
            return "pending"
        return "pending"

    def _wata_status_priority(self, status: str) -> int:
        priorities = {
            "paid": 0,
            "pending": 1,
            "declined": 2,
            "expired": 3,
        }
        return priorities.get(status, 9)

    def _wata_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.wata_api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _wata_base_url(self) -> str:
        return (self.settings.wata_api_base_url or "https://api.wata.pro/api/h2h").rstrip("/")

    def _wata_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._wata_base_url(),
            headers=self._wata_headers(),
            timeout=self.settings.wata_timeout_seconds,
        )
