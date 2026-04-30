from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from altlink.domain.enums import PlanCode, PromoRewardKind
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
async def test_ensure_client_access_includes_real_agreement_link(test_services):
    test_services.settings.backend_public_url = "https://altlink.online"
    message = DummyMessage(text="/start", user_id=21009)

    async with test_services.hub() as hub:
        user = await client_handlers.ensure_client_access(message, test_services, hub)

    assert user is None
    agreement_markup = message.answers[0]["reply_markup"]
    buttons = [button for row in agreement_markup.inline_keyboard for button in row]
    open_button = next(button for button in buttons if button.url)
    assert open_button.url == "https://altlink.online/legal/agreement"


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
async def test_ensure_client_access_allows_manual_maintenance_exception(test_services):
    message = DummyMessage(text="Меню", user_id=21010)

    async with test_services.hub() as hub:
        created = await client_handlers.ensure_user(message.from_user, test_services, hub)
        await hub.accounts.complete_registration(created.id)
        await hub.accounts.mark_channel_verified(created.id)
        await hub.accounts.mark_promo_onboarding_completed(created.id)
        await hub.monitoring.set_manual_client_maintenance(True)
        await hub.monitoring.add_manual_maintenance_exception(created)

    async with test_services.hub() as hub:
        user = await client_handlers.ensure_client_access(message, test_services, hub)

    assert user is not None
    assert user.id == created.id
    assert message.answers == []


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


def test_portal_login_resume_url_points_back_to_login_page_with_token():
    settings = Settings(_env_file=None, backend_public_url="https://altlink.online")

    url = client_handlers.portal_login_resume_url(settings, "demo-token")

    assert url == "https://altlink.online/portal/login?token=demo-token"


def test_home_text_keeps_menu_compact_and_mentions_cabinet_button():
    settings = Settings(_env_file=None, backend_public_url="https://altlink.online")
    user = SimpleNamespace(balance_rub=Decimal("199.00"), status="active")
    subscription = SimpleNamespace(
        plan=SimpleNamespace(name="Pro", period_days=30),
        notes=None,
    )

    text = client_handlers.home_text(user, subscription, settings)

    assert "Сайт: https://altlink.online" not in text
    assert "Кабинет: https://altlink.online/portal" not in text
    assert "✨ Всё управление VPN доступно кнопками ниже." in text


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

    assert "💳 Баланс: 99.00 ₽" in text
    assert "Тариф: Pro" in text
    assert "Сайт: https://altlink.online" not in text
    assert "Кабинет: https://altlink.online/portal" not in text
    assert "NL Node" not in text
    assert "Подключение:" not in text
    assert "Сервер:" not in text
    assert "Telegram ID" not in text
    assert "Формат списания" not in text
    assert "Лимит устройств" not in text


def test_subscription_text_hides_billing_cycle_line():
    settings = Settings(_env_file=None, backend_public_url="https://altlink.online")
    user = SimpleNamespace(balance_rub=Decimal("99.00"), status="active")
    subscription = SimpleNamespace(
        plan=SimpleNamespace(name="Pro", code=PlanCode.UNLIMITED, device_limit=8),
        auto_renew=True,
        next_billing_at=datetime(2026, 1, 1, 12, 0, 0),
        notes=None,
        traffic_used_bytes=0,
        whitelist_traffic_used_bytes=0,
    )

    text = client_handlers.subscription_text(
        {"user": user, "subscription": subscription},
        user_servers=[],
        settings=settings,
    )

    assert "Тариф: Pro" in text
    assert "Формат списания" not in text


def test_subscription_link_caption_uses_telegram_code_formatting():
    caption = client_handlers.subscription_link_caption("https://sub.example/demo?x=1&y=2")

    assert "<code>https://sub.example/demo?x=1&amp;y=2</code>" in caption
    assert "удобно копировать через меню Telegram" not in caption


@pytest.mark.asyncio
async def test_try_edit_tracked_client_card_preserves_parse_mode_for_captions():
    captured: dict[str, object] = {}

    async def fake_edit_message_caption(*, chat_id, message_id, caption, reply_markup=None, parse_mode=None):
        captured["chat_id"] = chat_id
        captured["message_id"] = message_id
        captured["caption"] = caption
        captured["parse_mode"] = parse_mode
        return None

    target = SimpleNamespace(
        chat=SimpleNamespace(id=4321),
        bot=SimpleNamespace(edit_message_caption=fake_edit_message_caption),
    )
    client_handlers.CLIENT_LAST_CARD[4321] = (99, True)

    try:
        result = await client_handlers.try_edit_tracked_client_card(
            target,
            "<b>formatted</b>",
            reply_markup=None,
            media_file=None,
            parse_mode="HTML",
        )
    finally:
        client_handlers.CLIENT_LAST_CARD.pop(4321, None)

    assert result is True
    assert captured["caption"] == "<b>formatted</b>"
    assert captured["parse_mode"] == "HTML"


def test_agreement_text_uses_link_instead_of_stub_when_available():
    text = client_handlers.agreement_text(consent_accepted=False, agreement_link_available=True)

    assert "\u041e\u0442\u043a\u0440\u043e\u0439\u0442\u0435 \u043f\u043e\u043b\u043d\u044b\u0439 \u0442\u0435\u043a\u0441\u0442 \u0441\u043e\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u044f \u043f\u043e \u043a\u043d\u043e\u043f\u043a\u0435 \u043d\u0438\u0436\u0435" in text
    assert "\u0415\u0441\u043b\u0438 \u0441\u0441\u044b\u043b\u043a\u0430 \u043d\u0435 \u043e\u0442\u043a\u0440\u044b\u0432\u0430\u0435\u0442\u0441\u044f" in text
    assert "\u0437\u0430\u0433\u043b\u0443\u0448\u043a" not in text


def test_topup_amount_confirmation_text_prompts_next_step():
    text = client_handlers.topup_amount_confirmation_text(Decimal("350"))

    assert "Сумма: 350.00 ₽" in text
    assert "Оплатить" in text
    assert "способ оплаты" in text


def test_topup_provider_selection_text_points_to_buttons_only():
    text = client_handlers.topup_provider_selection_text(Decimal("350"), ["yookassa"])

    assert "Сумма: 350.00 ₽" in text
    assert "YooKassa" not in text
    assert "Нажмите на удобный способ оплаты ниже." in text


def test_available_topup_provider_codes_follow_resolved_provider():
    assert client_handlers.available_topup_provider_codes("yookassa", "yookassa") == ["yookassa", "manual"]
    assert client_handlers.available_topup_provider_codes("manual", "manual") == ["manual"]
    assert client_handlers.available_topup_provider_codes("yookassa", "stub") == ["stub", "manual"]


def test_topup_provider_status_text_explains_missing_yookassa_settings():
    text = client_handlers.topup_provider_status_text(
        configured_provider="yookassa",
        resolved_provider="stub",
        missing_settings=["YOOKASSA_SHOP_ID", "YOOKASSA_SECRET_KEY"],
    )

    assert "YooKassa выбрана как касса" in text
    assert "YOOKASSA_SHOP_ID" in text
    assert "YOOKASSA_SECRET_KEY" in text
    assert "тестовая заглушка" in text


def test_balance_topup_status_text_reflects_live_yookassa():
    text = client_handlers.balance_topup_status_text(
        configured_provider="yookassa",
        resolved_provider="yookassa",
        missing_settings=[],
    )

    assert text == "Пополнение доступно через YooKassa."


def test_balance_topup_status_text_reflects_stub_fallback():
    text = client_handlers.balance_topup_status_text(
        configured_provider="yookassa",
        resolved_provider="stub",
        missing_settings=["YOOKASSA_SECRET_KEY"],
    )

    assert "YOOKASSA_SECRET_KEY" in text
    assert "тестовая заглушка" in text


def test_balance_topup_status_text_reflects_support_flow():
    text = client_handlers.balance_topup_status_text(
        configured_provider="manual",
        resolved_provider="manual",
        missing_settings=[],
    )

    assert "через поддержку" in text


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
async def test_activate_plan_with_insufficient_balance_shows_topup_actions(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_answer_or_edit(target, text, *, reply_markup=None, **kwargs):
        captured["target"] = target
        captured["text"] = text
        captured["reply_markup"] = reply_markup
        captured["kwargs"] = kwargs
        return None

    async def fake_ensure_client_access(callback, container, hub):
        return SimpleNamespace(id="user-42")

    class DummyBilling:
        async def activate_paid_plan(self, user_id, plan_code, charge_user=True):
            raise client_handlers.ConflictError("РќРµРґРѕСЃС‚Р°С‚РѕС‡РЅРѕ СЃСЂРµРґСЃС‚РІ.")

    class DummyAccounts:
        async def get_current_subscription(self, user_id):
            return None

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(billing=DummyBilling(), accounts=DummyAccounts())

    monkeypatch.setattr(client_handlers, "answer_or_edit", fake_answer_or_edit)
    monkeypatch.setattr(client_handlers, "ensure_client_access", fake_ensure_client_access)

    callback = SimpleNamespace(data=f"client:activate_plan:{PlanCode.UNLIMITED.value}")
    container = SimpleNamespace(hub=fake_hub)

    await client_handlers.activate_plan(callback, container)

    assert "РќРµРґРѕСЃС‚Р°С‚РѕС‡РЅРѕ СЃСЂРµРґСЃС‚РІ." in str(captured["text"])
    markup = captured["reply_markup"]
    buttons = [button for row in markup.inline_keyboard for button in row]
    callbacks = [button.callback_data for button in buttons if button.callback_data]
    assert "client:topup_menu" in callbacks
    assert "client:plan_menu" in callbacks


@pytest.mark.asyncio
async def test_plan_menu_v2_uses_new_descriptions(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_answer_or_edit(target, text, *, reply_markup=None, **kwargs):
        captured["text"] = text
        captured["reply_markup"] = reply_markup
        captured["kwargs"] = kwargs
        return None

    async def fake_ensure_client_access(callback, container, hub):
        return SimpleNamespace(id="user-42")

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace()

    monkeypatch.setattr(client_handlers, "answer_or_edit", fake_answer_or_edit)
    monkeypatch.setattr(client_handlers, "ensure_client_access", fake_ensure_client_access)

    callback = SimpleNamespace(data="client:plan_menu")
    container = SimpleNamespace(hub=fake_hub)

    await client_handlers.plan_menu_v2(callback, container)

    text = str(captured["text"])
    assert captured["kwargs"]["parse_mode"] == "HTML"
    assert "<b>🌍 Тарифы ALTLINK VPN</b>" in text
    assert "<b>Start</b>" in text
    assert "<b>Pro</b>" in text
    assert "🟢" not in text
    assert "🟡" not in text
    assert "До 2 устройств" in text
    assert "До 8 устройств" in text
    assert "серверы со скоростью до 10 Гбит/с" in text
    assert "🛡️ Белые списки доступны отдельно: 4 ₽ за 1 ГБ" in text
    assert "🛡️ Обход белых списков без ограничений" in text
    assert "на мобильном интернете работают только отдельные российские сервисы" in text
    assert "ALTLINK VPN обходит эти ограничения и возвращает доступ к привычным сервисам!" in text


@pytest.mark.asyncio
async def test_plan_family_menu_uses_updated_copy(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_answer_or_edit(target, text, *, reply_markup=None, **kwargs):
        captured["text"] = text
        captured["reply_markup"] = reply_markup
        captured["kwargs"] = kwargs
        return None

    async def fake_ensure_client_access(callback, container, hub):
        return SimpleNamespace(id="user-42")

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace()

    monkeypatch.setattr(client_handlers, "answer_or_edit", fake_answer_or_edit)
    monkeypatch.setattr(client_handlers, "ensure_client_access", fake_ensure_client_access)

    callback = SimpleNamespace(data="client:plan_family:unlimited", answer=AsyncMock())
    container = SimpleNamespace(hub=fake_hub)

    await client_handlers.plan_family_menu(callback, container)

    text = str(captured["text"])
    assert captured["kwargs"]["parse_mode"] == "HTML"
    assert "<b>Pro</b>" in text
    assert "До 8 устройств" in text
    assert "Безлимитный трафик на всех серверах" in text
    assert "Разные локации для выбора под ваш маршрут" in text
    assert "🛡️ Обход белых списков без ограничений" in text
    assert "на мобильном интернете работают только отдельные российские сервисы" in text
    assert "ALTLINK VPN обходит эти ограничения и возвращает доступ к привычным сервисам!" in text


@pytest.mark.asyncio
async def test_plan_family_menu_for_start_explains_whitelist_bypass(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_answer_or_edit(target, text, *, reply_markup=None, **kwargs):
        captured["text"] = text
        captured["reply_markup"] = reply_markup
        captured["kwargs"] = kwargs
        return None

    async def fake_ensure_client_access(callback, container, hub):
        return SimpleNamespace(id="user-42")

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace()

    monkeypatch.setattr(client_handlers, "answer_or_edit", fake_answer_or_edit)
    monkeypatch.setattr(client_handlers, "ensure_client_access", fake_ensure_client_access)

    callback = SimpleNamespace(data="client:plan_family:10gbit", answer=AsyncMock())
    container = SimpleNamespace(hub=fake_hub)

    await client_handlers.plan_family_menu(callback, container)

    text = str(captured["text"])
    assert captured["kwargs"]["parse_mode"] == "HTML"
    assert "<b>Start</b>" in text
    assert "До 2 устройств" in text
    assert "Безлимитный трафик на основном сервере" in text
    assert "⚡ Серверы сети рассчитаны на скорость до 10 Гбит/с" in text
    assert "Что такое обход белых списков" in text
    assert "4 ₽ за 1 ГБ" in text


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
        async def is_client_maintenance_active(self, telegram_id=None):
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
