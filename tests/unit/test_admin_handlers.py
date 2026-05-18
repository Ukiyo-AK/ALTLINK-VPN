from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
async def test_maintenance_toggle_updates_manual_state(monkeypatch):
    rendered_states: list[dict] = []
    state = {"enabled": False, "exceptions": []}

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_get_admin_by_telegram_id(telegram_id: int):
        return SimpleNamespace(id="admin-1")

    async def fake_get_manual_client_maintenance_state():
        return dict(state)

    async def fake_set_manual_client_maintenance(enabled: bool, *, actor_admin_id: str | None = None):
        state["enabled"] = enabled
        state["updated_by_admin_id"] = actor_admin_id
        return dict(state)

    async def fake_render_manual_maintenance_screen(target, *, container, manual_state: dict):
        rendered_states.append(dict(manual_state))

    class DummyCallback:
        from_user = SimpleNamespace(id=42)

        async def answer(self, *args, **kwargs):
            return None

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            accounts=SimpleNamespace(get_admin_by_telegram_id=fake_get_admin_by_telegram_id),
            monitoring=SimpleNamespace(
                get_manual_client_maintenance_state=fake_get_manual_client_maintenance_state,
                set_manual_client_maintenance=fake_set_manual_client_maintenance,
            ),
        )

    container = SimpleNamespace(hub=fake_hub, settings=SimpleNamespace(remnawave_base_url="https://panel.example.com"))
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_manual_maintenance_screen", fake_render_manual_maintenance_screen)

    await admin_handlers.maintenance_toggle(DummyCallback(), container)

    assert state["enabled"] is True
    assert state["updated_by_admin_id"] == "admin-1"
    assert rendered_states
    assert rendered_states[0]["enabled"] is True


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
async def test_servers_screen_syncs_catalog_before_render(monkeypatch):
    sync_servers = AsyncMock()
    rendered: list[str] = []
    sent_cards: list[str] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_render(target, text: str, reply_markup=None, **kwargs):
        rendered.append(text)

    class DummyMessage:
        from_user = SimpleNamespace(id=42)

        async def answer(self, text: str, reply_markup=None, **kwargs):
            sent_cards.append(text)
            return SimpleNamespace(message_id=1, chat=SimpleNamespace(id=42))

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            catalog=SimpleNamespace(
                sync_servers=sync_servers,
                list_servers=AsyncMock(return_value=[SimpleNamespace(id="server-1", is_available=True)]),
            )
        )

    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render)
    monkeypatch.setattr(admin_handlers, "format_server_card", lambda server: f"Server {server.id}")
    monkeypatch.setattr(admin_handlers, "remember_admin_card", lambda message: None)

    await admin_handlers.servers_screen(DummyMessage(), SimpleNamespace(hub=fake_hub))

    sync_servers.assert_awaited_once()
    assert rendered
    assert sent_cards == ["Server server-1"]


@pytest.mark.asyncio
async def test_toggle_server_handles_missing_local_server(monkeypatch):
    rendered: list[str] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_render(target, text: str, reply_markup=None, **kwargs):
        rendered.append(text)

    class DummyCallback:
        data = "admin:server_toggle:server-1:0"
        from_user = SimpleNamespace(id=42)
        message = SimpleNamespace()

        async def answer(self, *args, **kwargs):
            return None

    async def fake_set_server_availability(server_id: str, is_available: bool):
        raise admin_handlers.NotFoundError("missing")

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(catalog=SimpleNamespace(set_server_availability=fake_set_server_availability))

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render)

    await admin_handlers.toggle_server(DummyCallback(), container)

    assert rendered
    assert "Сервер уже удалён из локальной базы" in rendered[0]


@pytest.mark.asyncio
async def test_change_server_type_handles_missing_local_server(monkeypatch):
    rendered: list[str] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_render(target, text: str, reply_markup=None, **kwargs):
        rendered.append(text)

    class DummyCallback:
        data = "admin:server_type:server-2:whitelist"
        from_user = SimpleNamespace(id=42)
        message = SimpleNamespace()

        async def answer(self, *args, **kwargs):
            return None

    async def fake_set_server_type(server_id: str, server_type):
        raise admin_handlers.NotFoundError("missing")

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(catalog=SimpleNamespace(set_server_type=fake_set_server_type))

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render)

    await admin_handlers.change_server_type(DummyCallback(), container)

    assert rendered
    assert "Сервер уже удалён из локальной базы" in rendered[0]


@pytest.mark.asyncio
async def test_confirm_force_delete_server_shows_warning(monkeypatch):
    rendered: list[tuple[str, object]] = []
    calls: list[tuple[str, object]] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_render(target, text: str, reply_markup=None, **kwargs):
        rendered.append((text, reply_markup))

    async def fake_get_server(server_id: str):
        calls.append(("get", server_id))
        return SimpleNamespace(
            id=server_id,
            name="Demo",
            address="demo.example.com",
            server_type=admin_handlers.ServerType.REGULAR,
            is_available=False,
            is_connected=False,
            current_clients=0,
            users_online=0,
            max_clients=0,
            load_percent=0,
        )

    class DummyCallback:
        data = f"{admin_handlers.SERVER_DELETE_PREFIX}:server-3"
        from_user = SimpleNamespace(id=42)

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(catalog=SimpleNamespace(get_server=fake_get_server))

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render)

    await admin_handlers.confirm_force_delete_server(DummyCallback(), container)

    assert ("get", "server-3") in calls
    assert rendered
    assert "Remnawave" in rendered[0][0]
    assert rendered[0][1] is not None


@pytest.mark.asyncio
async def test_force_delete_server_callback_calls_catalog(monkeypatch):
    rendered: list[str] = []
    calls: list[tuple[str, object]] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_render(target, text: str, reply_markup=None, **kwargs):
        rendered.append(text)

    async def fake_force_delete_server(server_id: str):
        calls.append(("delete", server_id))
        return {
            "name": "Demo",
            "address": "demo.example.com",
            "assigned_users": 2,
            "accesses": 3,
            "inbounds": 1,
        }

    class DummyCallback:
        data = f"{admin_handlers.SERVER_DELETE_CONFIRM_PREFIX}:server-3"
        from_user = SimpleNamespace(id=42)

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(catalog=SimpleNamespace(force_delete_server=fake_force_delete_server))

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render)

    await admin_handlers.force_delete_server(DummyCallback(), container)

    assert ("delete", "server-3") in calls
    assert rendered
    assert "Demo" in rendered[0]


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
async def test_top_users_menu_syncs_traffic_before_building_rating(monkeypatch):
    rendered: list[str] = []
    calls: list[str] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_snapshot_traffic():
        calls.append("sync")

    async def fake_top_users(metric: str):
        calls.append(metric)
        return [SimpleNamespace(user=SimpleNamespace(username="leader", telegram_id=101), value=12 * 1024**3)]

    async def fake_render_admin(target, text: str, **kwargs):
        rendered.append(text)

    class DummyMessage:
        from_user = SimpleNamespace(id=42)

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            billing=SimpleNamespace(snapshot_traffic=fake_snapshot_traffic),
            dashboard=SimpleNamespace(top_users=fake_top_users),
        )

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render_admin)

    await admin_handlers.top_users_menu(DummyMessage(), container)

    assert calls == ["sync", "traffic"]
    assert rendered
    assert "leader" in rendered[0]


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
async def test_broadcast_confirm_sends_sticker_and_text(monkeypatch):
    sent_stickers: list[tuple[str, int, str]] = []
    sent_messages: list[tuple[str, int, str]] = []
    state_cleared = False

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_list_user_targets():
        return [SimpleNamespace(id="u1", telegram_id=707, username="sticker_user")]

    async def fake_log_event(**payload):
        return None

    async def fake_render_admin(target, text: str, **kwargs):
        return None

    class DummyClientBot:
        def __init__(self, token: str):
            self.token = token
            self.session = SimpleNamespace(close=self.close)

        async def send_sticker(self, chat_id: int, sticker):
            sent_stickers.append((self.token, chat_id, getattr(sticker, "filename", "")))

        async def send_message(self, chat_id: int, text: str):
            sent_messages.append((self.token, chat_id, text))

        async def close(self):
            return None

    class DummyAdminBot:
        async def download(self, file, destination):
            destination.write(b"sticker-bytes")
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
                "broadcast_text": "stickers are live",
                "broadcast_attachment": {
                    "kind": "sticker",
                    "file_id": "sticker-file-id",
                    "filename": "pack.webp",
                },
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

    assert sent_stickers == [("client-token", 707, "pack.webp")]
    assert sent_messages == [("client-token", 707, "stickers are live")]
    assert state_cleared is True


@pytest.mark.asyncio
async def test_payment_approve_updates_request_card(monkeypatch):
    rendered: list[str] = []
    approved_with: list[tuple[str, str | None, str | None]] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_get_admin_by_telegram_id(telegram_id: int):
        return SimpleNamespace(id="admin-1")

    async def fake_get_request(request_id: str):
        return SimpleNamespace(id=request_id, provider_code="manual", status="new")

    async def fake_approve(request_id: str, admin_id: str | None, comment: str | None = None):
        approved_with.append((request_id, admin_id, comment))
        return SimpleNamespace(
            id=request_id,
            status="approved",
            provider_code="manual",
            amount_rub=150,
            created_at=__import__("datetime").datetime(2026, 4, 25, 10, 0),
            user=SimpleNamespace(telegram_id=999, username="payer"),
            user_comment=None,
            admin_comment=comment,
        )

    async def fake_list_requests():
        return [
            SimpleNamespace(
                id="req-1",
                status="approved",
                provider_code="manual",
                amount_rub=150,
                created_at=__import__("datetime").datetime(2026, 4, 25, 10, 0),
                user=SimpleNamespace(telegram_id=999, username="payer"),
                user_comment=None,
                admin_comment="approved in admin bot",
            )
        ]

    async def fake_sync_pending_yookassa_checkouts():
        return 0

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
            topups=SimpleNamespace(
                get_request=fake_get_request,
                approve=fake_approve,
                list_requests=fake_list_requests,
                sync_pending_yookassa_checkouts=fake_sync_pending_yookassa_checkouts,
            ),
        )

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render_admin)

    await admin_handlers.payment_approve(DummyCallback(), container)

    assert approved_with == [("req-1", "admin-1", "???????????? ? admin bot")]
    assert rendered
    assert "1/1" in rendered[0]
    assert "req-1" in rendered[0]


@pytest.mark.asyncio
async def test_payment_approve_skips_manual_controls_for_yookassa(monkeypatch):
    callback_answers: list[tuple[str, bool]] = []
    approved_with: list[tuple[str, str | None, str | None]] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_get_request(request_id: str):
        return SimpleNamespace(id=request_id, provider_code="yookassa", status="new")

    async def fake_approve(request_id: str, admin_id: str | None, comment: str | None = None):
        approved_with.append((request_id, admin_id, comment))

    class DummyCallback:
        data = "adm:pa:req-yoo"
        from_user = SimpleNamespace(id=42)

        async def answer(self, text: str = "", show_alert: bool = False, **kwargs):
            callback_answers.append((text, show_alert))
            return None

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            accounts=SimpleNamespace(get_admin_by_telegram_id=lambda telegram_id: None),
            topups=SimpleNamespace(get_request=fake_get_request, approve=fake_approve),
        )

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)

    await admin_handlers.payment_approve(DummyCallback(), container)

    assert approved_with == []
    assert callback_answers == [("??? ?????? ??? ?????? ????????????? ?? ?????.", True)]


@pytest.mark.asyncio
async def test_payments_screen_renders_single_browser_message(monkeypatch):
    rendered: list[str] = []
    extra_answers: list[str] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_list_requests():
        return [
            SimpleNamespace(
                id="req-1",
                status="new",
                provider_code="manual",
                amount_rub=150,
                created_at=__import__("datetime").datetime(2026, 4, 25, 10, 0),
                user=SimpleNamespace(telegram_id=999, username="payer1"),
                user_comment=None,
                admin_comment=None,
            ),
            SimpleNamespace(
                id="req-2",
                status="approved",
                provider_code="manual",
                amount_rub=300,
                created_at=__import__("datetime").datetime(2026, 4, 24, 10, 0),
                user=SimpleNamespace(telegram_id=555, username="payer2"),
                user_comment=None,
                admin_comment="ok",
            ),
        ]

    async def fake_sync_pending_yookassa_checkouts():
        return 0

    async def fake_render_admin(target, text: str, **kwargs):
        rendered.append(text)

    class DummyMessage:
        from_user = SimpleNamespace(id=42)

        async def answer(self, text: str, reply_markup=None, **kwargs):
            extra_answers.append(text)
            return self

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            topups=SimpleNamespace(
                list_requests=fake_list_requests,
                sync_pending_yookassa_checkouts=fake_sync_pending_yookassa_checkouts,
            )
        )

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render_admin)

    await admin_handlers.payments_screen(DummyMessage(), container)

    assert len(rendered) == 1
    assert extra_answers == []
    assert "1/2" in rendered[0]
    assert "req-1" in rendered[0]


def test_format_payment_browser_marks_yookassa_new_as_unfinished():
    items = [
        SimpleNamespace(
            id="req-yoo",
            status="new",
            provider_code="yookassa",
            amount_rub=199,
            created_at=__import__("datetime").datetime(2026, 4, 25, 10, 0),
            user=SimpleNamespace(telegram_id=999, username="payer1"),
            user_comment=None,
            admin_comment=None,
        ),
        SimpleNamespace(
            id="req-manual",
            status="new",
            provider_code="manual",
            amount_rub=300,
            created_at=__import__("datetime").datetime(2026, 4, 24, 10, 0),
            user=SimpleNamespace(telegram_id=555, username="payer2"),
            user_comment=None,
            admin_comment=None,
        ),
    ]

    text = admin_handlers.format_payment_browser(items, 0)

    assert "\u041d\u0435\u0437\u0430\u0432\u0435\u0440\u0448\u0451\u043d\u043d\u044b\u0435 \u042e\u043a\u0430\u0441\u0441\u0430 \u0421\u0411\u041f: 1" in text
    assert "\u041e\u0436\u0438\u0434\u0430\u044e\u0442 \u0440\u0435\u0448\u0435\u043d\u0438\u044f: 1" in text
    assert admin_handlers.payment_status_label(items[0]) in text
    assert admin_handlers.payment_provider_label(items[0]) in text
    assert "req-yoo" in text


@pytest.mark.asyncio
async def test_user_direct_message_submit_sends_via_client_bot(monkeypatch):
    rendered: list[str] = []
    sent_messages: list[tuple[str, int, str]] = []
    logged_payloads: list[dict] = []
    state_cleared = False

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_get_user(user_id: str):
        return SimpleNamespace(id=user_id, telegram_id=404, username="client404")

    async def fake_get_admin_by_telegram_id(telegram_id: int):
        return SimpleNamespace(id="admin-1")

    async def fake_log_event(**payload):
        logged_payloads.append(payload)

    async def fake_render_admin(target, text: str, **kwargs):
        rendered.append(text)

    class DummyClientBot:
        def __init__(self, token: str):
            self.token = token
            self.session = SimpleNamespace(close=self.close)

        async def send_message(self, chat_id: int, text: str):
            sent_messages.append((self.token, chat_id, text))

        async def close(self):
            return None

    class DummyMessage:
        text = "Нужно проверить подключение после обновления."
        from_user = SimpleNamespace(id=42)

        async def answer(self, text: str, reply_markup=None, **kwargs):
            rendered.append(text)
            return self

    class DummyState:
        async def get_data(self):
            return {"direct_message_user_id": "user-404"}

        async def clear(self):
            nonlocal state_cleared
            state_cleared = True

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            accounts=SimpleNamespace(
                get_user=fake_get_user,
                get_admin_by_telegram_id=fake_get_admin_by_telegram_id,
                log_event=fake_log_event,
            )
        )

    container = SimpleNamespace(settings=SimpleNamespace(client_bot_token="client-token"), hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render_admin)
    monkeypatch.setattr(admin_handlers, "Bot", DummyClientBot)

    await admin_handlers.user_direct_message_submit(DummyMessage(), DummyState(), container)

    assert sent_messages == [("client-token", 404, "Нужно проверить подключение после обновления.")]
    assert state_cleared is True
    assert "Личное сообщение отправлено." in rendered[0]
    assert logged_payloads[0]["event_type"] == "direct_message_sent"


@pytest.mark.asyncio
async def test_user_direct_message_submit_sends_document_with_caption(monkeypatch):
    rendered: list[str] = []
    sent_documents: list[tuple[str, int, str, str]] = []
    logged_payloads: list[dict] = []
    state_cleared = False

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_get_user(user_id: str):
        return SimpleNamespace(id=user_id, telegram_id=505, username="client505")

    async def fake_get_admin_by_telegram_id(telegram_id: int):
        return SimpleNamespace(id="admin-1")

    async def fake_log_event(**payload):
        logged_payloads.append(payload)

    async def fake_render_admin(target, text: str, **kwargs):
        rendered.append(text)

    class DummyClientBot:
        def __init__(self, token: str):
            self.token = token
            self.session = SimpleNamespace(close=self.close)

        async def send_document(self, chat_id: int, document, caption: str | None = None):
            sent_documents.append((self.token, chat_id, caption or "", getattr(document, "filename", "")))

        async def close(self):
            return None

    class DummyAdminBot:
        async def download(self, file, destination):
            destination.write(b"pdf-bytes")
            destination.seek(0)
            return destination

    class DummyMessage:
        text = None
        caption = "Вот инструкция"
        document = SimpleNamespace(file_id="doc-file-id", file_name="guide.pdf")
        from_user = SimpleNamespace(id=42)
        bot = DummyAdminBot()

        async def answer(self, text: str, reply_markup=None, **kwargs):
            rendered.append(text)
            return self

    class DummyState:
        async def get_data(self):
            return {"direct_message_user_id": "user-505"}

        async def clear(self):
            nonlocal state_cleared
            state_cleared = True

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            accounts=SimpleNamespace(
                get_user=fake_get_user,
                get_admin_by_telegram_id=fake_get_admin_by_telegram_id,
                log_event=fake_log_event,
            )
        )

    container = SimpleNamespace(settings=SimpleNamespace(client_bot_token="client-token"), hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render_admin)
    monkeypatch.setattr(admin_handlers, "Bot", DummyClientBot)

    await admin_handlers.user_direct_message_submit(DummyMessage(), DummyState(), container)

    assert sent_documents == [("client-token", 505, "Вот инструкция", "guide.pdf")]
    assert state_cleared is True
    assert "Личное сообщение отправлено." in rendered[0]
    assert logged_payloads[0]["payload"]["attachment_kind"] == "document"


@pytest.mark.asyncio
async def test_database_backup_export_sends_document(monkeypatch):
    sent_documents: list[tuple[str, str]] = []
    callback_answers: list[str] = []

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_export_database():
        return SimpleNamespace(
            filename="altlink-backup.json",
            content=b'{"format":"altlink-db-backup-v1"}',
            summary={
                "format": "altlink-db-backup-v1",
                "exported_at": "2026-04-27T12:00:00+00:00",
                "database_dialect": "postgresql",
                "table_counts": {"users": 3},
                "total_rows": 3,
            },
        )

    class DummyMessage:
        async def answer_document(self, document, caption: str | None = None, **kwargs):
            sent_documents.append((getattr(document, "filename", ""), caption or ""))
            return self

    class DummyCallback:
        from_user = SimpleNamespace(id=42)
        message = DummyMessage()

        async def answer(self, text: str = "", **kwargs):
            callback_answers.append(text)

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(backups=SimpleNamespace(export_database=fake_export_database))

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)

    await admin_handlers.database_backup_export(DummyCallback(), container)

    assert sent_documents
    assert sent_documents[0][0] == "altlink-backup.json"
    assert "Экспорт базы готов" in sent_documents[0][1]
    assert callback_answers == ["Резервная копия отправлена."]


@pytest.mark.asyncio
async def test_database_backup_import_confirm_replaces_database(monkeypatch):
    imported_payloads: list[bytes] = []
    rendered: list[str] = []
    state_cleared = False

    async def fake_is_admin(telegram_id: int, container) -> bool:
        return True

    async def fake_import_database(payload: bytes):
        imported_payloads.append(payload)
        return {
            "format": "altlink-db-backup-v1",
            "exported_at": "2026-04-27T12:00:00+00:00",
            "database_dialect": "postgresql",
            "table_counts": {"users": 2, "admin_users": 1},
            "total_rows": 3,
        }

    async def fake_render_admin(target, text: str, **kwargs):
        rendered.append(text)

    class DummyCallback:
        from_user = SimpleNamespace(id=42)

        async def answer(self, *args, **kwargs):
            return None

    class DummyState:
        async def clear(self):
            nonlocal state_cleared
            state_cleared = True

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(backups=SimpleNamespace(import_database=fake_import_database))

    container = SimpleNamespace(hub=fake_hub)
    monkeypatch.setattr(admin_handlers, "is_admin", fake_is_admin)
    monkeypatch.setattr(admin_handlers, "render_admin", fake_render_admin)
    admin_handlers.set_pending_database_import(42, b'{"format":"altlink-db-backup-v1"}')

    await admin_handlers.database_backup_import_confirm(DummyCallback(), DummyState(), container)

    assert imported_payloads == [b'{"format":"altlink-db-backup-v1"}']
    assert state_cleared is True
    assert admin_handlers.get_pending_database_import(42) is None
    assert "Импорт базы завершён" in rendered[0]
    assert "Текущая локальная база заменена" in rendered[0]
