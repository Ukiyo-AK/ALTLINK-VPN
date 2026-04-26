from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update

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


@pytest.mark.asyncio
async def test_ensure_client_access_sends_agreement_and_channel_messages_separately(test_services):
    message = DummyMessage(text="/start", user_id=21001)

    async with test_services.hub() as hub:
        user = await client_handlers.ensure_client_access(message, test_services, hub)

    assert user is None
    assert len(message.answers) == 2
    assert message.answers[0]["text"].startswith("Шаг 1 из 2. Пользовательское соглашение")
    assert message.answers[1]["text"].startswith("Шаг 2 из 2. Подписка на канал")


@pytest.mark.asyncio
async def test_ensure_client_access_returns_user_after_channel_verification_and_consent(test_services):
    message = DummyMessage(text="Меню", user_id=21002)

    async with test_services.hub() as hub:
        created = await client_handlers.ensure_user(message.from_user, test_services, hub)
        await hub.accounts.complete_registration(created.id)
        await hub.accounts.mark_channel_verified(created.id)

    async with test_services.hub() as hub:
        user = await client_handlers.ensure_client_access(message, test_services, hub)

    assert user is not None
    assert user.id == created.id
    assert message.answers == []


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
async def test_send_reply_menu_uses_hidden_helper_message():
    message = DummyMessage(text="/start", user_id=21004)

    await client_handlers.send_reply_menu(message)

    assert len(message.answers) == 1
    assert message.answers[0]["text"] == "\u2060"


@pytest.mark.asyncio
async def test_notify_admins_about_topup_request_sends_to_admin_bot(monkeypatch):
    sent: list[tuple[str, int, str, object]] = []

    class DummyBot:
        def __init__(self, token: str):
            self.token = token
            self.session = SimpleNamespace(close=self.close)

        async def send_message(self, chat_id: int, text: str, reply_markup=None):
            sent.append((self.token, chat_id, text, reply_markup))

        async def close(self):
            return None

    monkeypatch.setattr(client_handlers, "Bot", DummyBot)
    container = SimpleNamespace(settings=SimpleNamespace(admin_bot_token="admin-token"))
    user = SimpleNamespace(telegram_id=777, username="payer")

    await client_handlers.notify_admins_about_topup_request(
        container,
        user=user,
        amount=Decimal("350"),
        request_id="req-77",
        admin_telegram_ids=[11, 22],
    )

    assert [item[:2] for item in sent] == [("admin-token", 11), ("admin-token", 22)]
    assert all("req-77" in item[2] for item in sent)
