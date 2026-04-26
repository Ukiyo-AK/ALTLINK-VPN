from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Message, Update

from altlink.presentation.bots import admin_handlers


@pytest.mark.asyncio
async def test_users_search_handles_reply_after_prompt(monkeypatch):
    bot = Bot("123456:ABCDEF")
    dispatcher = Dispatcher()
    dispatcher.include_router(admin_handlers.router)
    answers: list[str] = []
    calls: list[tuple[str, str | None]] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return telegram_id == 42

    async def fake_list_users(query: str | None = None):
        calls.append(("search", query))
        if query is None:
            return [SimpleNamespace(id="recent-1", username="recent", telegram_id=77)]
        return [SimpleNamespace(id="user-1", username="demo", telegram_id=123456)]

    async def fake_show_user_card(target, user_id: str, container):
        calls.append(("card", user_id))

    async def fake_answer(self, text: str, reply_markup=None, **kwargs):
        answers.append(text)
        return self

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(accounts=SimpleNamespace(list_users=fake_list_users))

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "show_user_card", fake_show_user_card)
    monkeypatch.setattr(Message, "answer", fake_answer, raising=False)

    prompt_update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 1710000000,
                "chat": {"id": 42, "type": "private", "first_name": "Admin"},
                "from": {"id": 42, "is_bot": False, "first_name": "Admin"},
                "text": "Пользователи",
            },
        }
    )
    search_update = Update.model_validate(
        {
            "update_id": 2,
            "message": {
                "message_id": 11,
                "date": 1710000001,
                "chat": {"id": 42, "type": "private", "first_name": "Admin"},
                "from": {"id": 42, "is_bot": False, "first_name": "Admin"},
                "text": "123456",
            },
        }
    )

    try:
        await dispatcher.feed_update(bot, prompt_update, container=container)
        await dispatcher.feed_update(bot, search_update, container=container)
    finally:
        await bot.session.close()

    assert answers[0].startswith("Введите Telegram ID")
    assert calls == [("search", None), ("search", "123456"), ("card", "user-1")]


@pytest.mark.asyncio
async def test_toggle_server_callback_parses_callback_data(monkeypatch):
    calls: list[tuple[str, object]] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    class DummyMessage:
        async def answer(self, text: str, reply_markup=None, **kwargs):
            calls.append(("message", text))

    class DummyCallback:
        data = "admin:server_toggle:server-1:0"
        from_user = SimpleNamespace(id=42)
        message = DummyMessage()

        async def answer(self, *args, **kwargs):
            calls.append(("callback", "answered"))

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(catalog=SimpleNamespace(set_server_availability=fake_set_server_availability))

    async def fake_set_server_availability(server_id: str, is_available: bool):
        calls.append(("toggle", (server_id, is_available)))

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)

    await admin_handlers.toggle_server(DummyCallback(), container)

    assert ("toggle", ("server-1", False)) in calls


@pytest.mark.asyncio
async def test_change_server_type_callback_parses_callback_data(monkeypatch):
    calls: list[tuple[str, object]] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    class DummyMessage:
        async def answer(self, text: str, reply_markup=None, **kwargs):
            calls.append(("message", text))

    class DummyCallback:
        data = "admin:server_type:server-2:whitelist"
        from_user = SimpleNamespace(id=42)
        message = DummyMessage()

        async def answer(self, *args, **kwargs):
            calls.append(("callback", "answered"))

    async def fake_set_server_type(server_id: str, server_type):
        calls.append(("type", (server_id, server_type.value)))
        return SimpleNamespace(name="Demo", server_type=server_type)

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(catalog=SimpleNamespace(set_server_type=fake_set_server_type))

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)

    await admin_handlers.change_server_type(DummyCallback(), container)

    assert ("type", ("server-2", "whitelist")) in calls


@pytest.mark.asyncio
async def test_show_user_card_formats_without_missing_attributes_exception():
    answers: list[str] = []

    class DummyTarget:
        async def answer(self, text: str, reply_markup=None, **kwargs):
            answers.append(text)

    async def fake_user_card(user_id: str):
        return {
            "user": SimpleNamespace(
                telegram_id=123456,
                username="demo",
                assigned_server=None,
                balance_rub=0,
                status="active",
            ),
            "subscription": None,
        }

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(accounts=SimpleNamespace(user_card=fake_user_card, is_registered=lambda user: True))

    container = SimpleNamespace(hub=fake_hub)

    await admin_handlers.show_user_card(DummyTarget(), "user-1", container)

    assert answers
    assert "Telegram ID: 123456" in answers[0]


@pytest.mark.asyncio
async def test_broadcast_confirm_uses_client_bot_and_reports_failures(monkeypatch):
    sent_messages: list[tuple[str, int, str]] = []
    logged_payloads: list[dict] = []
    rendered: list[str] = []
    state_cleared = False

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_list_user_targets():
        return [
            SimpleNamespace(id="u1", telegram_id=101, username="first"),
            SimpleNamespace(id="u2", telegram_id=202, username="second"),
        ]

    async def fake_log_event(**payload):
        logged_payloads.append(payload)

    async def fake_render_admin(target, text: str, **kwargs):
        rendered.append(text)

    class DummyClientBot:
        def __init__(self, token: str):
            self.token = token
            self.session = SimpleNamespace(close=self.close)

        async def send_message(self, chat_id: int, text: str):
            if chat_id == 202:
                raise Exception("Telegram server says - Bad Request: chat not found")
            sent_messages.append((self.token, chat_id, text))

        async def send_photo(self, chat_id: int, photo, caption: str):
            sent_messages.append((self.token, chat_id, caption))

        async def close(self):
            return None

    class DummyAdminBot:
        async def download(self, file, destination):
            destination.write(b"image")
            destination.seek(0)
            return destination

    class DummyCallback:
        from_user = SimpleNamespace(id=42)
        bot = DummyAdminBot()

        async def answer(self, *args, **kwargs):
            return None

    class DummyState:
        async def get_data(self):
            return {
                "broadcast_text": "service update",
                "broadcast_file_id": None,
                "broadcast_use_default": False,
            }

        async def clear(self):
            nonlocal state_cleared
            state_cleared = True

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(accounts=SimpleNamespace(list_user_targets=fake_list_user_targets, log_event=fake_log_event))

    container = SimpleNamespace(settings=SimpleNamespace(client_bot_token="client-token"), hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render_admin)
    monkeypatch.setattr(admin_handlers, "Bot", DummyClientBot)

    await admin_handlers.broadcast_confirm(DummyCallback(), DummyState(), container)

    assert sent_messages == [("client-token", 101, "service update")]
    assert state_cleared is True
    assert "Отправлено: 1" in rendered[0]
    assert "чат с клиентским ботом не открыт" in rendered[0]
    assert logged_payloads[0]["payload"]["failure_reasons"] == {"чат с клиентским ботом не открыт": 1}


@pytest.mark.asyncio
async def test_broadcast_confirm_downloads_admin_photo_for_client_bot(monkeypatch):
    sent_photos: list[tuple[str, int, str, str]] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_list_user_targets():
        return [SimpleNamespace(id="u1", telegram_id=303, username="photo_user")]

    async def fake_log_event(**payload):
        return None

    async def fake_render_admin(target, text: str, **kwargs):
        return None

    class DummyClientBot:
        def __init__(self, token: str):
            self.token = token
            self.session = SimpleNamespace(close=self.close)

        async def send_message(self, chat_id: int, text: str):
            raise AssertionError("text path should not be used")

        async def send_photo(self, chat_id: int, photo, caption: str):
            sent_photos.append((self.token, chat_id, caption, getattr(photo, "filename", "")))

        async def close(self):
            return None

    class DummyAdminBot:
        async def download(self, file, destination):
            destination.write(b"png-bytes")
            destination.seek(0)
            return destination

    class DummyCallback:
        from_user = SimpleNamespace(id=42)
        bot = DummyAdminBot()

        async def answer(self, *args, **kwargs):
            return None

    class DummyState:
        async def get_data(self):
            return {
                "broadcast_text": "photo update",
                "broadcast_file_id": "photo-file-id",
                "broadcast_use_default": False,
            }

        async def clear(self):
            return None

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(accounts=SimpleNamespace(list_user_targets=fake_list_user_targets, log_event=fake_log_event))

    container = SimpleNamespace(settings=SimpleNamespace(client_bot_token="client-token"), hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render_admin)
    monkeypatch.setattr(admin_handlers, "Bot", DummyClientBot)

    await admin_handlers.broadcast_confirm(DummyCallback(), DummyState(), container)

    assert sent_photos
    assert sent_photos[0][0] == "client-token"
    assert sent_photos[0][1] == 303
    assert sent_photos[0][2] == "photo update"
    assert sent_photos[0][3].startswith("broadcast-")


@pytest.mark.asyncio
async def test_payment_approve_updates_request_card(monkeypatch):
    rendered: list[str] = []
    approved_with: list[tuple[str, str | None, str | None]] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_get_admin_by_telegram_id(telegram_id: int):
        return SimpleNamespace(id="admin-1")

    async def fake_approve(request_id: str, admin_id: str | None, comment: str | None = None):
        approved_with.append((request_id, admin_id, comment))
        return SimpleNamespace(
            id=request_id,
            status="approved",
            amount_rub=150,
            created_at=__import__("datetime").datetime(2026, 4, 25, 10, 0),
            user=SimpleNamespace(telegram_id=999, username="payer"),
            user_comment=None,
            admin_comment=comment,
        )

    async def fake_render_admin(target, text: str, **kwargs):
        rendered.append(text)

    class DummyCallback:
        data = "adm:pa:req-1"
        from_user = SimpleNamespace(id=42)

        async def answer(self, *args, **kwargs):
            return None

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            accounts=SimpleNamespace(get_admin_by_telegram_id=fake_get_admin_by_telegram_id),
            topups=SimpleNamespace(approve=fake_approve),
        )

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render_admin)

    await admin_handlers.payment_approve(DummyCallback(), container)

    assert approved_with == [("req-1", "admin-1", "Подтверждено в admin bot")]
    assert rendered
    assert "Статус: подтверждён" in rendered[0]
