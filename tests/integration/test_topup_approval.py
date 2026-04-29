from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import json

import httpx
import pytest
from sqlalchemy import select

from altlink.application.services.base import ConflictError
from altlink.application.services.topups import TopupService
from altlink.domain.enums import BalanceTransactionType, PlanCode
from altlink.infrastructure.db.models import BalanceTransaction
from altlink.utils.time import utc_now


@pytest.mark.asyncio
async def test_stub_topup_adds_money_and_notification(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3001,
            username="paying",
            first_name="Pay",
            last_name="User",
            language_code="ru",
        )
        request = await hub.topups.create_request(user.id, Decimal("350"), auto_complete=True)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(3001)
        requests = await hub.topups.list_requests(user_id=user.id)
        assert Decimal(user.balance_rub) == Decimal("350")
        assert request.id == requests[0].id
        assert requests[0].status == "approved"
        pending = await hub.notifications.pending_query()
        queued = list((await hub.session.scalars(pending)).all())
        assert any(item.user_id == user.id for item in queued)


@pytest.mark.asyncio
async def test_manual_topup_stays_pending_until_admin_approval(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3002,
            username="manual_pay",
            first_name="Manual",
            last_name="User",
            language_code="ru",
        )
        request = await hub.topups.create_request(user.id, Decimal("275"), auto_complete=False)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(3002)
        requests = await hub.topups.list_requests(user_id=user.id)
        assert Decimal(user.balance_rub) == Decimal("0")
        assert requests[0].id == request.id
        assert str(requests[0].status) == "new"

        approved = await hub.topups.approve(request.id, admin_id=None, comment="manual approve")
        user = await hub.accounts.get_user_by_telegram_id(3002)
        assert str(approved.status) == "approved"
        assert Decimal(user.balance_rub) == Decimal("275")


@pytest.mark.asyncio
async def test_topup_checkout_stays_manual_by_default(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3003,
            username="manual_checkout",
            first_name="Manual",
            last_name="Checkout",
            language_code="ru",
        )
        checkout = await hub.topups.create_checkout(user.id, Decimal("150"))

    async with test_services.hub() as hub:
        refreshed = await hub.accounts.get_user_by_telegram_id(3003)
        request = await hub.topups.get_request(checkout.request.id)
        assert hub.topups.resolved_provider() == "manual"
        assert checkout.provider == "manual"
        assert checkout.admin_required is True
        assert checkout.auto_completed is False
        assert str(request.status) == "new"
        assert Decimal(refreshed.balance_rub) == Decimal("0")


@pytest.mark.asyncio
async def test_yookassa_without_credentials_falls_back_to_stub_checkout(test_services):
    test_services.settings.payment_provider = "yookassa"
    test_services.settings.yookassa_shop_id = ""
    test_services.settings.yookassa_secret_key = ""

    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3004,
            username="yookassa_stub",
            first_name="Yoo",
            last_name="Stub",
            language_code="ru",
        )
        checkout = await hub.topups.create_checkout(user.id, Decimal("125"))

    async with test_services.hub() as hub:
        refreshed = await hub.accounts.get_user_by_telegram_id(3004)
        request = await hub.topups.get_request(checkout.request.id)
        assert hub.topups.yookassa_missing_settings() == ["YOOKASSA_SHOP_ID", "YOOKASSA_SECRET_KEY"]
        assert hub.topups.resolved_provider() == "stub"
        assert checkout.provider == "stub"
        assert checkout.auto_completed is True
        assert str(request.status) == "approved"
        assert Decimal(refreshed.balance_rub) == Decimal("125")


@pytest.mark.asyncio
async def test_yookassa_checkout_creates_redirect_payment_and_approves_after_status_poll(test_services, monkeypatch):
    test_services.settings.payment_provider = "yookassa"
    test_services.settings.yookassa_shop_id = "shop-123"
    test_services.settings.yookassa_secret_key = "secret-456"

    created_payloads: list[dict] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization", "").startswith("Basic ")
        if request.method == "POST" and request.url.path.endswith("/payments"):
            created_payloads.append(json.loads(request.content.decode("utf-8")))
            assert request.headers.get("Idempotence-Key")
            return httpx.Response(
                200,
                json={
                    "id": "pay-demo-1",
                    "status": "pending",
                    "confirmation": {
                        "type": "redirect",
                        "confirmation_url": "https://pay.yookassa.example/confirm/pay-demo-1",
                    },
                },
            )
        if request.method == "GET" and request.url.path.endswith("/payments/pay-demo-1"):
            return httpx.Response(
                200,
                json={
                    "id": "pay-demo-1",
                    "status": "succeeded",
                    "confirmation": {
                        "type": "redirect",
                        "confirmation_url": "https://pay.yookassa.example/confirm/pay-demo-1",
                    },
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    def fake_client(self):
        return httpx.AsyncClient(
            base_url=self._yookassa_base_url(),
            transport=httpx.MockTransport(transport_handler),
            auth=httpx.BasicAuth(self.settings.yookassa_shop_id, self.settings.yookassa_secret_key),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=self.settings.yookassa_timeout_seconds,
        )

    monkeypatch.setattr(TopupService, "_yookassa_client", fake_client)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3006,
            username="yookassa_live",
            first_name="Yoo",
            last_name="Kassa",
            language_code="ru",
        )
        checkout = await hub.topups.create_checkout(user.id, Decimal("410"))

        assert checkout.provider == "yookassa"
        assert checkout.payment_url == "https://pay.yookassa.example/confirm/pay-demo-1"
        assert checkout.request.external_payment_id == "pay-demo-1"
        assert checkout.request.external_payment_url == "https://pay.yookassa.example/confirm/pay-demo-1"

    async with test_services.hub() as hub:
        snapshot = await hub.topups.check_checkout_status(checkout.request.id)
        refreshed = await hub.accounts.get_user_by_telegram_id(3006)
        stored_request = await hub.topups.get_request(checkout.request.id)

    assert created_payloads
    assert created_payloads[0]["amount"]["value"] == "410.00"
    assert created_payloads[0]["confirmation"]["type"] == "redirect"
    assert snapshot.provider == "yookassa"
    assert snapshot.is_paid is True
    assert snapshot.is_final is True
    assert str(stored_request.status) == "approved"
    assert stored_request.external_payment_id == "pay-demo-1"
    assert Decimal(refreshed.balance_rub) == Decimal("410")


@pytest.mark.asyncio
async def test_yookassa_mode_still_allows_manual_support_checkout(test_services, monkeypatch):
    test_services.settings.payment_provider = "yookassa"
    test_services.settings.yookassa_shop_id = "shop-123"
    test_services.settings.yookassa_secret_key = "secret-456"

    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3007,
            username="support_checkout",
            first_name="Support",
            last_name="Checkout",
            language_code="ru",
        )
        checkout = await hub.topups.create_checkout(user.id, Decimal("260"), provider_code="manual")
        request = await hub.topups.get_request(checkout.request.id)

    assert checkout.provider == "manual"
    assert checkout.admin_required is True
    assert checkout.auto_completed is False
    assert str(request.status) == "new"


@pytest.mark.asyncio
async def test_topup_checkout_rejects_amounts_below_minimum(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3008,
            username="too_small",
            first_name="Too",
            last_name="Small",
            language_code="ru",
        )
        with pytest.raises(ConflictError, match="Минимальная сумма пополнения — 50 ₽"):
            await hub.topups.create_checkout(user.id, Decimal("49"))


@pytest.mark.asyncio
async def test_plan_switch_keeps_compensation_message_only_in_transaction_history(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=3005,
            username="carryover_once",
            first_name="Carry",
            last_name="Over",
            language_code="ru",
        )
        await hub.accounts.adjust_balance(
            user_id=user.id,
            amount_rub=Decimal("500"),
            transaction_type=BalanceTransactionType.TOPUP,
            description="Seed balance",
        )
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        current = await hub.accounts.get_current_subscription(user.id)
        assert current is not None
        current.ends_at = utc_now() + timedelta(days=15)
        current.next_billing_at = current.ends_at

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(3005)
        switched = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED, charge_user=True)
        transactions = list(
            (
                await hub.session.scalars(
                    select(BalanceTransaction)
                    .where(BalanceTransaction.user_id == user.id)
                    .order_by(BalanceTransaction.created_at.asc())
                )
            ).all()
        )

    compensation_descriptions = [
        item.description
        for item in transactions
        if item.type == BalanceTransactionType.REFUND
        and item.description == "Компенсация остатка прошлого тарифа при смене плана"
    ]
    assert compensation_descriptions == ["Компенсация остатка прошлого тарифа при смене плана"]
    assert switched.notes is None or "Компенсация остатка прошлого тарифа" not in switched.notes
