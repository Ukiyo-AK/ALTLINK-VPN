from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from altlink.domain.enums import PromoRewardKind
from altlink.presentation.bots import client_handlers
from altlink.settings import Settings


class DummyMessage:
    def __init__(self, *, text: str, user_id: int, username: str = "demo_user") -> None:
        self.text = text
        self.from_user = SimpleNamespace(
            id=user_id,
            username=username,
            first_name="Demo",
            last_name="User",
            language_code="ru",
        )
        self.answers: list[dict[str, object]] = []

    async def answer(self, text: str, reply_markup=None):
        payload = {"text": text, "reply_markup": reply_markup}
        self.answers.append(payload)
        return self

    async def delete(self):
        return None


class DummyState:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.state: str | None = None

    async def set_state(self, value) -> None:
        self.state = str(value)

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)

    async def clear(self) -> None:
        self.data.clear()
        self.state = None


@pytest.mark.asyncio
async def test_ensure_client_access_sends_agreement_and_channel_messages_separately(test_services):
    message = DummyMessage(text="/start", user_id=21001)

    async with test_services.hub() as hub:
        user = await client_handlers.ensure_client_access(message, test_services, hub)

    assert user is None
    assert len(message.answers) == 2
    assert "Шаг 1 из 3" in str(message.answers[0]["text"])
    assert "Шаг 2 из 3" in str(message.answers[1]["text"])


@pytest.mark.asyncio
async def test_ensure_client_access_returns_user_after_channel_verification_and_consent(test_services):
    message = DummyMessage(text="Меню", user_id=21002)

    async with test_services.hub() as hub:
        created = await client_handlers.ensure_user(message.from_user, test_services, hub)
        await hub.accounts.complete_registration(created.id)
        await hub.accounts.mark_channel_verified(created.id)
        await hub.accounts.mark_promo_onboarding_completed(created.id)

    async with test_services.hub() as hub:
        user = await client_handlers.ensure_client_access(message, test_services, hub)

    assert user is not None
    assert user.id == created.id
    assert message.answers == []


@pytest.mark.asyncio
async def test_ensure_client_access_shows_promo_step_after_registration_and_channel_verification(test_services):
    message = DummyMessage(text="Меню", user_id=21006)

    async with test_services.hub() as hub:
        created = await client_handlers.ensure_user(message.from_user, test_services, hub)
        await hub.accounts.complete_registration(created.id)
        await hub.accounts.mark_channel_verified(created.id)

    async with test_services.hub() as hub:
        user = await client_handlers.ensure_client_access(message, test_services, hub)

    assert user is None
    assert len(message.answers) == 1
    assert "Шаг 3 из 3" in str(message.answers[0]["text"])


@pytest.mark.asyncio
async def test_start_with_portal_login_token_shows_confirmation_prompt(test_services):
    message = DummyMessage(text="/start login_demo-token", user_id=21008)

    async with test_services.hub() as hub:
        attempt = await hub.portal_auth.create_login_attempt()
        attempt.token = "demo-token"

    await client_handlers.start(message, test_services)

    assert len(message.answers) == 1
    assert "Telegram" in str(message.answers[0]["text"])
    markup = message.answers[0]["reply_markup"]
    assert markup is not None
    callback_data = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "client:portal_login_confirm:demo-token" in callback_data
    assert "client:portal_login_cancel:demo-token" in callback_data


@pytest.mark.asyncio
async def test_dispatch_menu_button_routes_to_main_action(monkeypatch):
    bot = Bot("123456:ABCDEF")
    dispatcher = Dispatcher()
    dispatcher.include_router(client_handlers.router)
    calls: list[tuple[str, str, str | None]] = []

    async def fake_access(message, container, hub=None):
        calls.append(("access", message.text, None))
        return SimpleNamespace(id="user-1")

    async def fake_perform(action, message, container, hub):
        calls.append(("action", action, message.text))

    class DummyHubContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    @asynccontextmanager
    async def fake_hub():
        context = DummyHubContext()
        try:
            yield await context.__aenter__()
        finally:
            await context.__aexit__(None, None, None)

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(client_handlers, "ensure_client_access", fake_access)
    monkeypatch.setattr(client_handlers, "perform_main_action", fake_perform)

    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 1710000000,
                "chat": {"id": 42, "type": "private", "first_name": "Test"},
                "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                "text": "Меню",
            },
        }
    )

    try:
        await dispatcher.feed_update(bot, update, container=container)
    finally:
        await bot.session.close()

    assert calls == [("access", "Меню", None), ("action", "menu", "Меню")]


def test_share_vpn_url_skips_invalid_targets():
    settings = Settings(
        _env_file=None,
        client_bot_name="Bot Display Name",
        backend_public_url="backend-without-scheme",
    )

    assert client_handlers.share_vpn_target_url(settings) is None
    assert client_handlers.share_vpn_url(settings) is None


def test_home_text_includes_site_links_in_main_menu():
    settings = Settings(_env_file=None, backend_public_url="https://altlink.online")
    user = SimpleNamespace(balance_rub=Decimal("199.00"), status="active")
    subscription = SimpleNamespace(
        plan=SimpleNamespace(name="Pro", period_days=30),
        notes=None,
    )

    text = client_handlers.home_text(user, subscription, settings)

    assert "Сайт: https://altlink.online" in text
    assert "Кабинет: https://altlink.online/portal" in text


def test_profile_text_keeps_only_key_details_and_links():
    settings = Settings(_env_file=None, backend_public_url="https://altlink.online")
    user = SimpleNamespace(
        balance_rub=Decimal("99.00"),
        assigned_server=SimpleNamespace(name="NL Node"),
    )
    subscription = SimpleNamespace(
        plan=SimpleNamespace(name="Pro", is_trial=False, period_days=30),
        auto_renew=True,
        next_billing_at=datetime(2026, 1, 1, 12, 0, 0),
    )

    text = client_handlers.profile_text(user, subscription, settings)

    assert "Баланс: 99.00 ₽" in text
    assert "Тариф: Pro" in text
    assert "Сервер: NL Node" in text
    assert "Сайт: https://altlink.online" in text
    assert "Кабинет: https://altlink.online/portal" in text
    assert "Telegram ID" not in text
    assert "Формат списания" not in text
    assert "Лимит устройств" not in text


def test_topup_amount_confirmation_text_prompts_next_step():
    text = client_handlers.topup_amount_confirmation_text(Decimal("350"))

    assert "Сумма: 350.00 ₽" in text
    assert "Оплатить" in text
    assert "способ оплаты" in text


def test_topup_provider_selection_text_lists_yookassa():
    text = client_handlers.topup_provider_selection_text(Decimal("350"), ["yookassa"])

    assert "Сумма: 350.00 ₽" in text
    assert "YooKassa" in text
    assert "Выберите вариант оплаты" in text


def test_available_topup_provider_codes_follow_resolved_provider():
    assert client_handlers.available_topup_provider_codes("yookassa") == ["yookassa"]
    assert client_handlers.available_topup_provider_codes("manual") == ["manual"]
    assert client_handlers.available_topup_provider_codes("stub") == ["stub"]


@pytest.mark.asyncio
async def test_get_access_state_auto_verifies_subscribed_user(test_services, monkeypatch):
    message = DummyMessage(text="Меню", user_id=21003)

    async with test_services.hub() as hub:
        created = await client_handlers.ensure_user(message.from_user, test_services, hub)
        await hub.accounts.complete_registration(created.id)

    async def fake_is_channel_member(telegram_id: int, container) -> bool:
        return telegram_id == 21003

    monkeypatch.setattr(client_handlers, "is_channel_member", fake_is_channel_member)

    async with test_services.hub() as hub:
        user, consent_ok, channel_ok = await client_handlers.get_access_state(message, test_services, hub)
        refreshed = await hub.accounts.get_user(user.id)

    assert consent_ok is True
    assert channel_ok is True
    assert refreshed.channel_verified_at is not None


@pytest.mark.asyncio
async def test_promo_submit_finishes_onboarding_after_success(test_services):
    message = DummyMessage(text="WELCOME100", user_id=21007)
    state = DummyState()

    async with test_services.hub() as hub:
        user = await client_handlers.ensure_user(message.from_user, test_services, hub)
        await hub.accounts.complete_registration(user.id)
        await hub.accounts.mark_channel_verified(user.id)
        await hub.promos.create_code(
            code="WELCOME100",
            name="Welcome promo",
            reward_kind=PromoRewardKind.BALANCE,
            reward_value=Decimal("100"),
            usage_limit=10,
            expires_at=None,
            new_users_only=False,
            admin_id=None,
        )

    await state.update_data(promo_source="onboarding")
    await client_handlers.promo_submit(message, state, test_services)

    async with test_services.hub() as hub:
        refreshed = await hub.accounts.get_user_by_telegram_id(21007)

    assert refreshed is not None
    assert refreshed.promo_onboarding_completed_at is not None
    assert refreshed.balance_rub == Decimal("100.00")
    assert any("Промокод применён" in str(item["text"]) for item in message.answers)


@pytest.mark.asyncio
async def test_send_reply_menu_uses_hidden_helper_message():
    message = DummyMessage(text="/start", user_id=21004)

    await client_handlers.send_reply_menu(message)

    assert len(message.answers) == 1
    assert message.answers[0]["text"] == "\u2060"


@pytest.mark.asyncio
async def test_notify_admins_about_topup_request_sends_to_admin_bot(monkeypatch):
    sent: list[dict[str, object]] = []

    async def fake_send_telegram_messages(*, bot_token: str, chat_ids, text: str, reply_markup=None):
        sent.append(
            {
                "bot_token": bot_token,
                "chat_ids": list(chat_ids),
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return len(list(chat_ids))

    monkeypatch.setattr(client_handlers, "send_telegram_messages", fake_send_telegram_messages)
    container = SimpleNamespace(settings=SimpleNamespace(admin_bot_token="admin-token"))
    user = SimpleNamespace(telegram_id=777, username="payer")

    await client_handlers.notify_admins_about_topup_request(
        container,
        user=user,
        amount=Decimal("350"),
        request_id="req-77",
        admin_telegram_ids=[11, 22],
    )

    assert sent[0]["bot_token"] == "admin-token"
    assert sent[0]["chat_ids"] == [11, 22]
    assert "req-77" in str(sent[0]["text"])


@pytest.mark.asyncio
async def test_ensure_client_access_shows_maintenance_stub_when_panel_is_unavailable():
    message = DummyMessage(text="Меню", user_id=21005)

    class DummyMonitoring:
        async def is_client_maintenance_active(self):
            return True

    container = SimpleNamespace(settings=SimpleNamespace(support_username="@altlink_support"))

    user = await client_handlers.ensure_client_access(
        message,
        container,
        SimpleNamespace(monitoring=DummyMonitoring()),
    )

    assert user is None
    assert len(message.answers) == 1
    assert "Технические работы" in str(message.answers[0]["text"])
