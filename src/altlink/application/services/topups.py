from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re
from urllib.parse import urlparse

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


TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
MIN_TOPUP_AMOUNT_RUB = Decimal("50")


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
        if configured == "yookassa":
            return "yookassa" if self._is_yookassa_configured() else "stub"
        if configured == "stub":
            return "stub"
        return "manual"

    def configured_provider(self) -> str:
        return (self.settings.payment_provider or "manual").strip().lower() or "manual"

    def yookassa_missing_settings(self) -> list[str]:
        missing: list[str] = []
        if not (self.settings.yookassa_api_base_url or "").strip():
            missing.append("YOOKASSA_API_BASE_URL")
        if not (self.settings.yookassa_shop_id or "").strip():
            missing.append("YOOKASSA_SHOP_ID")
        if not (self.settings.yookassa_secret_key or "").strip():
            missing.append("YOOKASSA_SECRET_KEY")
        return missing

    def available_checkout_providers(self) -> list[str]:
        configured = self.configured_provider()
        resolved = self.resolved_provider()
        if configured == "manual":
            return ["manual"]

        providers: list[str] = []
        if resolved in {"yookassa", "stub"}:
            providers.append(resolved)
        providers.append("manual")
        return list(dict.fromkeys(providers))

    async def create_checkout(
        self,
        user_id: str,
        amount_rub: Decimal,
        comment: str | None = None,
        *,
        provider_code: str | None = None,
    ) -> TopupCheckoutSession:
        provider = (provider_code or self.resolved_provider()).strip().lower()
        if provider not in self.available_checkout_providers():
            raise ConflictError("Этот способ пополнения сейчас недоступен.")
        if self.configured_provider() == "yookassa" and provider == "stub":
            await self.log_event(
                level=SystemEventLevel.WARNING,
                event_type="topup_provider_fallback_stub",
                message="Юкасса СБП включена, но не настроена полностью. Использована заглушка.",
                payload={"missing_settings": self.yookassa_missing_settings()},
            )
        if provider == "yookassa":
            request = await self.create_request(
                user_id,
                amount_rub,
                comment=comment,
                auto_complete=False,
                provider_code=provider,
            )
            payment_id, payment_url = await self._create_yookassa_payment(request)
            request.external_payment_id = payment_id
            request.external_payment_url = payment_url
            await self.session.flush()
            return TopupCheckoutSession(
                request=request,
                provider=provider,
                payment_url=payment_url,
                admin_required=False,
                auto_completed=False,
            )
        if provider == "manual":
            request = await self.create_request(
                user_id,
                amount_rub,
                comment=comment,
                auto_complete=False,
                provider_code=provider,
            )
            return TopupCheckoutSession(
                request=request,
                provider=provider,
                payment_url=None,
                admin_required=True,
                auto_completed=False,
            )

        request = await self.create_request(
            user_id,
            amount_rub,
            comment=comment,
            auto_complete=True,
            provider_code="stub",
        )
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
        provider_code: str | None = None,
    ) -> TopupRequest:
        if amount_rub < MIN_TOPUP_AMOUNT_RUB:
            raise ConflictError(f"Минимальная сумма пополнения — {MIN_TOPUP_AMOUNT_RUB:.0f} ₽.")
        request = TopupRequest(
            user_id=user_id,
            amount_rub=Decimal(amount_rub),
            user_comment=comment,
            provider_code=(provider_code or self.resolved_provider()),
        )
        self.session.add(request)
        await self.session.flush()
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="topup_created",
            message="Создан новый платёж на пополнение.",
            payload={
                "topup_request_id": request.id,
                "user_id": user_id,
                "amount": str(amount_rub),
                "provider": request.provider_code,
            },
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
        if admin_id is not None and self._request_provider(item) != "manual":
            return item
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
            payload={"topup_request_id": item.id, "provider": self._request_provider(item)},
            actor_admin_id=admin_id,
        )
        return item

    async def reject(self, request_id: str, admin_id: str | None, comment: str | None = None) -> TopupRequest:
        item = await self.get_request(request_id)
        if item.status != TopupStatus.NEW:
            raise ConflictError("Отклонить можно только новый платёж.")
        if admin_id is not None and self._request_provider(item) != "manual":
            return item
        item.status = TopupStatus.REJECTED
        item.admin_comment = comment
        item.approved_by_admin_id = admin_id
        item.rejected_at = utc_now()
        await self.notifications.queue(
            user_id=item.user_id,
            notification_type=NotificationType.TOPUP_REJECTED,
            message=topup_rejected_message(
                Decimal(item.amount_rub),
                None if self._request_provider(item) == "yookassa" else comment,
            ),
            dedupe_key=f"topup-rejected:{item.id}",
        )
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="topup_rejected",
            message="Платёж на пополнение отклонён.",
            payload={"topup_request_id": item.id, "provider": self._request_provider(item)},
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
        provider = self._request_provider(request)

        if request.status == TopupStatus.APPROVED:
            return TopupStatusSnapshot(
                request=request,
                provider=provider,
                payment_url=request.external_payment_url,
                external_status="approved",
                is_paid=True,
                is_final=True,
                is_stub=provider == "stub",
            )
        if request.status in {TopupStatus.REJECTED, TopupStatus.CANCELED}:
            return TopupStatusSnapshot(
                request=request,
                provider=provider,
                payment_url=request.external_payment_url,
                external_status=str(request.status),
                is_paid=False,
                is_final=True,
                is_stub=False,
            )
        if provider != "yookassa":
            return TopupStatusSnapshot(
                request=request,
                provider=provider,
                payment_url=request.external_payment_url,
                external_status="stub" if provider == "stub" else "manual",
                is_paid=provider == "stub",
                is_final=provider == "stub",
                is_stub=provider == "stub",
            )

        if not request.external_payment_id:
            return TopupStatusSnapshot(
                request=request,
                provider=provider,
                payment_url=request.external_payment_url,
                external_status="pending",
                is_paid=False,
                is_final=False,
            )

        payment = await self._fetch_yookassa_payment(request.external_payment_id)
        payment_url = self._extract_yookassa_confirmation_url(payment) or request.external_payment_url
        if payment_url and payment_url != request.external_payment_url:
            request.external_payment_url = payment_url
        external_status = self._normalize_yookassa_status(payment.get("status"))

        if external_status == "paid":
            if request.status == TopupStatus.NEW:
                request = await self.approve(request.id, admin_id=None, comment=f"yookassa:{request.external_payment_id}")
            return TopupStatusSnapshot(
                request=request,
                provider=provider,
                payment_url=payment_url,
                external_status=external_status,
                is_paid=True,
                is_final=True,
            )

        if external_status == "canceled" and request.status == TopupStatus.NEW:
            cancellation_reason = str((payment.get("cancellation_details") or {}).get("reason") or "canceled")
            request = await self.reject(request.id, admin_id=None, comment=f"yookassa:{cancellation_reason}")

        return TopupStatusSnapshot(
            request=request,
            provider=provider,
            payment_url=payment_url,
            external_status=external_status,
            is_paid=False,
            is_final=external_status == "canceled",
        )

    async def sync_pending_yookassa_checkouts(self, *, limit: int = 100) -> int:
        pending_requests = await self.list_requests(status=TopupStatus.NEW)
        processed = 0
        for request in pending_requests[:limit]:
            if self._request_provider(request) != "yookassa":
                continue
            if not request.external_payment_id:
                continue
            await self.check_checkout_status(request.id)
            processed += 1
        return processed

    def _request_provider(self, request: TopupRequest) -> str:
        provider = (request.provider_code or "").strip().lower()
        if provider in {"manual", "stub", "yookassa"}:
            return provider
        return "manual"

    def _is_yookassa_configured(self) -> bool:
        return not self.yookassa_missing_settings()

    async def _create_yookassa_payment(self, request: TopupRequest) -> tuple[str, str]:
        payload = {
            "amount": {
                "value": f"{Decimal(request.amount_rub):.2f}",
                "currency": self.settings.default_currency,
            },
            "capture": True,
            "confirmation": {
                "type": "redirect",
                "return_url": self._yookassa_return_url(),
            },
            "description": f"ALTLINK balance top-up #{request.id}",
            "metadata": {
                "topup_request_id": request.id,
                "user_id": request.user_id,
            },
        }

        async with self._yookassa_client() as client:
            response = await client.post(
                "/payments",
                json=payload,
                headers={"Idempotence-Key": request.id},
            )
            response.raise_for_status()
            data = response.json() or {}

        payment_id = str(data.get("id") or "").strip()
        payment_url = self._extract_yookassa_confirmation_url(data)
        if not payment_id or not payment_url:
            raise ConflictError("Юкасса СБП не вернула данные для оплаты.")
        return payment_id, payment_url

    async def _fetch_yookassa_payment(self, payment_id: str) -> dict:
        async with self._yookassa_client() as client:
            response = await client.get(f"/payments/{payment_id}")
            response.raise_for_status()
            return response.json() or {}

    def _extract_yookassa_confirmation_url(self, payload: dict | None) -> str | None:
        confirmation = (payload or {}).get("confirmation") or {}
        url = confirmation.get("confirmation_url")
        return str(url).strip() if url else None

    def _normalize_yookassa_status(self, raw_status: object) -> str:
        normalized = str(raw_status or "").strip().lower()
        if normalized == "succeeded":
            return "paid"
        if normalized in {"canceled", "cancelled"}:
            return "canceled"
        if normalized in {"pending", "waiting_for_capture"}:
            return "pending"
        return "pending"

    def _yookassa_return_url(self) -> str:
        custom = (self.settings.yookassa_return_url or "").strip()
        if custom:
            return custom

        bot_name = (self.settings.client_bot_name or "").strip().lstrip("@")
        if bot_name and TELEGRAM_USERNAME_RE.fullmatch(bot_name):
            return f"https://t.me/{bot_name}"

        backend_public_url = (self.settings.backend_public_url or "").strip()
        parsed = urlparse(backend_public_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return backend_public_url.rstrip("/")

        return "https://t.me"

    def _yookassa_base_url(self) -> str:
        return (self.settings.yookassa_api_base_url or "https://api.yookassa.ru/v3").rstrip("/")

    def _yookassa_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._yookassa_base_url(),
            auth=httpx.BasicAuth(
                (self.settings.yookassa_shop_id or "").strip(),
                (self.settings.yookassa_secret_key or "").strip(),
            ),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=self.settings.yookassa_timeout_seconds,
        )
