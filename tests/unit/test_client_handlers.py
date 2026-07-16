from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock

import httpx
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


class DummyCallbackMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=777)
        self.message_id = 55
        self.edits: list[dict[str, object]] = []
        self.answers: list[dict[str, object]] = []

    async def edit_text(self, text: str, reply_markup=None):
        payload = {"text": text, "reply_markup": reply_markup}
        self.edits.append(payload)
        return SimpleNamespace(chat=self.chat, message_id=self.message_id)

    async def answer(self, text: str, reply_markup=None):
        payload = {"text": text, "reply_markup": reply_markup}
        self.answers.append(payload)
        return SimpleNamespace(chat=self.chat, message_id=self.message_id)


class DummyCallback:
    def __init__(self) -> None:
        self.message = DummyCallbackMessage()
        self.callback_answers: list[dict[str, object]] = []

    async def answer(self, text: str | None = None, show_alert: bool | None = None, url: str | None = None):
        self.callback_answers.append({"text": text, "show_alert": show_alert, "url": url})
        return None


def _paid_subscription_stub(*, plan_code: PlanCode = PlanCode.UNLIMITED, plan_name: str = "Pro"):
    return SimpleNamespace(
        plan=SimpleNamespace(
            code=plan_code,
            name=plan_name,
            period_days=30,
            device_limit=8,
            is_trial=False,
        ),
        next_billing_at=datetime(2026, 5, 18, 12, 0),
        auto_renew=True,
    )


@pytest.mark.asyncio
async def test_ensure_client_access_sends_combined_channel_and_agreement_step(test_services):
    message = DummyMessage(text="/start", user_id=21001)

    async with test_services.hub() as hub:
        user = await client_handlers.ensure_client_access(message, test_services, hub)

    assert user is None
    assert len(message.answers) == 1
    assert "Шаг 1 из 2" in str(message.answers[0]["text"])
    assert "Продолжая пользоваться ботом" in str(message.answers[0]["text"])
    assert "пользовательским соглашением" in str(message.answers[0]["text"])


@pytest.mark.asyncio
async def test_ensure_client_access_includes_real_agreement_link(test_services):
    test_services.settings.backend_public_url = "https://altlink.online"
    message = DummyMessage(text="/start", user_id=21009)

    async with test_services.hub() as hub:
        user = await client_handlers.ensure_client_access(message, test_services, hub)

    assert user is None
    channel_markup = message.answers[0]["reply_markup"]
    buttons = [button for row in channel_markup.inline_keyboard for button in row]
    agreement_button = next(button for button in buttons if button.url == "https://altlink.online/legal/agreement")
    assert agreement_button.text == "📘 Пользовательское соглашение"


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
    assert "Шаг 2 из 2" in str(message.answers[0]["text"])


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


def test_referral_share_vpn_url_contains_only_link():
    settings = Settings(_env_file=None, client_bot_name="@Altlinkbot")

    url = client_handlers.referral_share_vpn_url(settings, "272B39BC")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "t.me"
    assert parsed.path == "/share/url"
    assert query == {"url": ["https://t.me/Altlinkbot?start=ref_272B39BC"]}


def test_portal_login_resume_url_points_back_to_login_page_with_token():
    settings = Settings(_env_file=None, backend_public_url="https://altlink.online")

    url = client_handlers.portal_login_resume_url(settings, "demo-token")

    assert url == "https://altlink.online/portal/login?token=demo-token"


def test_home_text_keeps_menu_compact_and_mentions_cabinet_button():
    settings = Settings(_env_file=None, backend_public_url="https://altlink.online")
    user = SimpleNamespace(balance_rub=Decimal("199.00"), status="active")
    subscription = SimpleNamespace(
        plan=SimpleNamespace(name="Pro", period_days=30, code=PlanCode.UNLIMITED),
        notes=None,
    )

    text = client_handlers.home_text(user, subscription, settings)

    assert "Сайт: https://altlink.online" not in text
    assert "Кабинет: https://altlink.online/portal" not in text
    assert "✨ Всё управление VPN доступно кнопками ниже." in text
    assert "БС:" not in text


def test_home_text_for_start_shows_whitelist_tariff_warning_and_totals():
    settings = Settings(_env_file=None, backend_public_url="https://altlink.online")
    user = SimpleNamespace(balance_rub=Decimal("42.50"), status="active")
    subscription = SimpleNamespace(
        plan=SimpleNamespace(name="Start", period_days=30, code=PlanCode.SINGLE_10GBIT),
        notes=None,
        whitelist_traffic_used_bytes=2 * 1024**3,
        whitelist_traffic_billed_bytes=2 * 1024**3,
    )

    text = client_handlers.home_text(user, subscription, settings)

    assert "⚠️ Start: белые списки тарифицируются отдельно — 2 ₽/ГБ." in text
    assert "При балансе -50 ₽ доступ к белым спискам временно закрывается." in text
    assert "БС: 2.00 ГБ • учтено 4.00 ₽" in text


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
    assert "Белые списки:" not in text


def test_profile_text_for_start_shows_whitelist_tariff_warning_and_totals():
    settings = Settings(_env_file=None, backend_public_url="https://altlink.online")
    user = SimpleNamespace(balance_rub=Decimal("42.50"))
    subscription = SimpleNamespace(
        plan=SimpleNamespace(name="Start", is_trial=False, code=PlanCode.SINGLE_10GBIT),
        auto_renew=True,
        next_billing_at=datetime(2026, 1, 1, 12, 0, 0),
        whitelist_traffic_used_bytes=2 * 1024**3,
        whitelist_traffic_billed_bytes=2 * 1024**3,
    )

    text = client_handlers.profile_text(user, subscription, settings)

    assert "⚠️ Start: белые списки тарифицируются отдельно — 2 ₽/ГБ." in text
    assert "При балансе -50 ₽ доступ к белым спискам временно закрывается." in text
    assert "БС: 2.00 ГБ • учтено 4.00 ₽" in text


def test_home_text_without_subscription_points_user_to_subscription_button():
    settings = Settings(_env_file=None, backend_public_url="https://altlink.online")
    user = SimpleNamespace(balance_rub=Decimal("15.00"), status="new")

    text = client_handlers.home_text(user, None, settings)

    assert "Тариф пока не выбран" in text
    assert "«Подписка»" in text
    assert "«Выбрать тариф»" in text


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


def test_subscription_text_for_start_on_whitelist_server_keeps_billing_details_without_push_warning():
    settings = Settings(_env_file=None, backend_public_url="https://altlink.online")
    user = SimpleNamespace(balance_rub=Decimal("42.50"), status="active")
    subscription = SimpleNamespace(
        plan=SimpleNamespace(name="Start", code=PlanCode.SINGLE_10GBIT, device_limit=2),
        auto_renew=True,
        next_billing_at=datetime(2026, 1, 1, 12, 0, 0),
        notes=None,
        traffic_used_bytes=5 * 1024**3,
        whitelist_traffic_used_bytes=2 * 1024**3,
        whitelist_traffic_billed_bytes=2 * 1024**3,
    )
    user_servers = [
        SimpleNamespace(
            status="active",
            server=SimpleNamespace(name="Whitelist EU", server_type=SimpleNamespace(value="whitelist")),
        ),
        SimpleNamespace(
            status="active",
            server=SimpleNamespace(name="Regular PL", server_type=SimpleNamespace(value="regular")),
        ),
    ]

    text = client_handlers.subscription_text(
        {"user": user, "subscription": subscription},
        user_servers=user_servers,
        settings=settings,
        activity_summary={"current_server_type": "whitelist", "recent_server_types": ["whitelist"]},
    )

    assert "Учтено за белые списки: 4.00 ₽" in text
    assert "Текущий баланс: 42.50 ₽" not in text
    assert "ТРАФИК ПО БЕЛЫМ СПИСКАМ СПИСЫВАЕТСЯ С БАЛАНСА СРАЗУ" not in text
    assert "Whitelist EU" not in text
    assert "Regular PL" not in text


def test_subscription_details_text_contains_servers_and_auto_renew_status():
    subscription = _paid_subscription_stub()
    user_servers = [
        SimpleNamespace(
            status="active",
            server=SimpleNamespace(name="Whitelist EU", server_type=SimpleNamespace(value="whitelist")),
        ),
        SimpleNamespace(
            status="active",
            server=SimpleNamespace(name="Regular PL", server_type=SimpleNamespace(value="regular")),
        ),
    ]

    text = client_handlers.subscription_details_text(subscription, user_servers)

    assert "Подробнее о подписке" in text
    assert "Автопродление: включено" in text
    assert "Whitelist EU • Белые списки • active" in text
    assert "Regular PL • Обычный • active" in text


def test_vless_keys_file_content_contains_each_key_on_separate_line():
    content = client_handlers.vless_keys_file_content(
        [
            "vless://first-key@server-one.example#NL%20Start",
            "vless://second-key@server-two.example#%D0%91%D0%B5%D0%BB%D1%8B%D0%B5%20%D1%81%D0%BF%D0%B8%D1%81%D0%BA%D0%B8",
            "vless://third-key@server-three.example",
        ]
    ).decode("utf-8")

    assert "ALTLINK VLESS keys" in content
    assert "1. Название конфига: NL Start" in content
    assert "Ключ: vless://first-key@server-one.example#NL%20Start" in content
    assert "2. Название конфига: Белые списки" in content
    assert "Ключ: vless://second-key@server-two.example#%D0%91%D0%B5%D0%BB%D1%8B%D0%B5%20%D1%81%D0%BF%D0%B8%D1%81%D0%BA%D0%B8" in content
    assert "3. Название конфига: Конфиг 3" in content


def test_subscription_link_caption_uses_telegram_code_formatting():
    caption = client_handlers.subscription_link_caption("https://sub.example/demo?x=1&y=2")

    assert "<code>https://sub.example/demo?x=1&amp;y=2</code>" in caption
    assert "удобно копировать через меню Telegram" not in caption


def test_resolve_subscription_payload_prefers_subscription_url_over_raw_keys():
    payload = client_handlers.resolve_subscription_payload(
        {
            "subscription_url": "https://sub.example/demo",
            "connection_keys": SimpleNamespace(enabledKeys=["vless://raw-config"]),
            "subscription_info": None,
        }
    )

    assert payload == "https://sub.example/demo"


def test_activation_success_caption_includes_copyable_link():
    subscription = SimpleNamespace(
        plan=SimpleNamespace(name="Pro", period_days=30, device_limit=8),
        next_billing_at=datetime(2026, 1, 1, 12, 0, 0),
    )

    caption = client_handlers.activation_success_caption(subscription, "https://sub.example/demo?x=1&y=2")

    assert "Тариф «Pro» активирован" in caption
    assert "<code>https://sub.example/demo?x=1&amp;y=2</code>" in caption
    assert "Ваша персональная ссылка VPN" in caption


@pytest.mark.asyncio
async def test_answer_or_edit_topup_checkout_skips_callback_redirect_for_external_urls(monkeypatch):
    callback = DummyCallback()
    monkeypatch.setattr(client_handlers, "CallbackQuery", DummyCallback)
    monkeypatch.setattr(client_handlers, "try_edit_tracked_client_card", AsyncMock(return_value=False))

    await client_handlers.answer_or_edit_topup_checkout(
        callback,
        "Checkout",
        payment_url="https://pay.yookassa.example/confirm/demo",
        request_id="req-1",
        can_check=True,
    )

    assert callback.callback_answers == [{"text": None, "show_alert": None, "url": None}]


@pytest.mark.asyncio
async def test_answer_or_edit_topup_checkout_keeps_callback_redirect_for_telegram_urls(monkeypatch):
    callback = DummyCallback()
    monkeypatch.setattr(client_handlers, "CallbackQuery", DummyCallback)
    monkeypatch.setattr(client_handlers, "try_edit_tracked_client_card", AsyncMock(return_value=False))

    await client_handlers.answer_or_edit_topup_checkout(
        callback,
        "Checkout",
        payment_url="https://t.me/altlink_support",
        request_id="req-1",
        can_check=False,
    )

    assert callback.callback_answers == [
        {"text": None, "show_alert": None, "url": "https://t.me/altlink_support"}
    ]


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


@pytest.mark.asyncio
async def test_try_edit_tracked_client_card_skips_other_message_ids():
    target = SimpleNamespace(
        message_id=120,
        chat=SimpleNamespace(id=4321),
        bot=SimpleNamespace(
            edit_message_text=AsyncMock(),
            edit_message_caption=AsyncMock(),
            edit_message_media=AsyncMock(),
        ),
    )
    client_handlers.CLIENT_LAST_CARD[4321] = (99, False)

    try:
        result = await client_handlers.try_edit_tracked_client_card(
            target,
            "updated",
            reply_markup=None,
            media_file=None,
            parse_mode=None,
        )
    finally:
        client_handlers.CLIENT_LAST_CARD.pop(4321, None)

    assert result is False
    target.bot.edit_message_text.assert_not_awaited()
    target.bot.edit_message_caption.assert_not_awaited()
    target.bot.edit_message_media.assert_not_awaited()


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


def test_topup_menu_text_lists_tariff_prices():
    text = client_handlers.topup_menu_text()

    assert "Выберите сумму пополнения." in text
    assert "Ориентир по тарифам:" in text
    assert "Start: 25 ₽ в неделю или 69 ₽ в месяц" in text
    assert "Pro: 65 ₽ в неделю или 199 ₽ в месяц" in text


def test_topup_plan_action_text_depends_on_current_subscription():
    paid_subscription = SimpleNamespace(plan=SimpleNamespace(is_trial=False))
    trial_subscription = SimpleNamespace(plan=SimpleNamespace(is_trial=True))

    assert client_handlers.topup_plan_action_text(None) == "🧾 Выбрать тариф"
    assert client_handlers.topup_plan_action_text(trial_subscription) == "🧾 Выбрать тариф"
    assert client_handlers.topup_plan_action_text(paid_subscription) == "🔄 Сменить тариф"


def test_topup_provider_selection_text_points_to_buttons_only():
    text = client_handlers.topup_provider_selection_text(Decimal("350"), ["yookassa"])

    assert "Сумма: 350.00 ₽" in text
    assert "YooKassa" not in text
    assert "Нажмите на удобный способ оплаты ниже." in text


def test_available_topup_provider_codes_follow_resolved_provider():
    assert client_handlers.available_topup_provider_codes("yookassa", "yookassa") == ["yookassa", "manual"]
    assert client_handlers.available_topup_provider_codes("manual", "manual") == ["manual"]
    assert client_handlers.available_topup_provider_codes("yookassa", "stub") == ["stub", "manual"]


@pytest.mark.asyncio
async def test_show_subscription_renders_for_active_trial_user(test_services):
    message = DummyMessage(text="Подписка", user_id=21055)

    async with test_services.hub() as hub:
        user = await client_handlers.ensure_user(message.from_user, test_services, hub)
        await hub.accounts.complete_registration(user.id)
        await hub.accounts.mark_channel_verified(user.id)
        await hub.accounts.mark_promo_onboarding_completed(user.id)
        await hub.billing.activate_trial(user.id)

    async with test_services.hub() as hub:
        await client_handlers.show_subscription(message, test_services, hub)

    assert len(message.answers) == 1
    assert "Подписка" in str(message.answers[0]["text"])
    assert "Тариф" in str(message.answers[0]["text"])


@pytest.mark.asyncio
async def test_show_subscription_tolerates_missing_remote_subscription_info(test_services, monkeypatch):
    message = DummyMessage(text="Подписка", user_id=21056)

    async with test_services.hub() as hub:
        user = await client_handlers.ensure_user(message.from_user, test_services, hub)
        await hub.accounts.complete_registration(user.id)
        await hub.accounts.mark_channel_verified(user.id)
        await hub.accounts.mark_promo_onboarding_completed(user.id)
        await hub.billing.activate_trial(user.id)
        short_uuid = user.remnawave_short_uuid

    async def missing_subscription_info(short_uuid_value: str):
        request = httpx.Request("GET", f"https://remna.example/api/sub/{short_uuid_value}/info")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr(test_services.remnawave, "get_subscription_info", missing_subscription_info)

    async with test_services.hub() as hub:
        await client_handlers.show_subscription(message, test_services, hub)

    assert len(message.answers) == 1
    assert "Подписка" in str(message.answers[0]["text"])
    assert "Тариф" in str(message.answers[0]["text"])
    assert short_uuid is not None


@pytest.mark.asyncio
async def test_continue_topup_flow_does_not_create_checkout_before_provider_selected(monkeypatch):
    captured: dict[str, object] = {}
    create_checkout = AsyncMock()
    list_requests = AsyncMock(return_value=[])

    async def fake_show_topup_provider_menu(target, amount, providers):
        captured["amount"] = amount
        captured["providers"] = providers

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            topups=SimpleNamespace(
                configured_provider=lambda: "yookassa",
                resolved_provider=lambda: "yookassa",
                create_checkout=create_checkout,
                list_requests=list_requests,
            ),
            accounts=SimpleNamespace(),
        )

    monkeypatch.setattr(client_handlers, "show_topup_provider_menu", fake_show_topup_provider_menu)
    container = SimpleNamespace(hub=fake_hub)

    await client_handlers.continue_topup_flow(
        DummyMessage(text="Пополнить", user_id=21011),
        container,
        user=SimpleNamespace(id="user-1"),
        amount=Decimal("350"),
    )

    create_checkout.assert_not_awaited()
    list_requests.assert_not_awaited()
    assert captured["amount"] == Decimal("350")
    assert captured["providers"] == ["yookassa", "manual"]


def test_topup_provider_status_text_explains_missing_yookassa_settings():
    text = client_handlers.topup_provider_status_text(
        configured_provider="yookassa",
        resolved_provider="stub",
        missing_settings=["YOOKASSA_SHOP_ID", "YOOKASSA_SECRET_KEY"],
    )

    assert "Юкасса СБП выбрана как касса" in text
    assert "YOOKASSA_SHOP_ID" in text
    assert "YOOKASSA_SECRET_KEY" in text
    assert "тестовая заглушка" in text


def test_balance_topup_status_text_reflects_live_yookassa():
    text = client_handlers.balance_topup_status_text(
        configured_provider="yookassa",
        resolved_provider="yookassa",
        missing_settings=[],
    )

    assert text == "Пополнение доступно через Юкасса СБП."


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
        await client_handlers.ensure_user(message.from_user, test_services, hub)

    async def fake_is_channel_member(telegram_id: int, container) -> bool:
        return telegram_id == 21003

    monkeypatch.setattr(client_handlers, "is_channel_member", fake_is_channel_member)

    async with test_services.hub() as hub:
        user, consent_ok, channel_ok = await client_handlers.get_access_state(message, test_services, hub)
        refreshed = await hub.accounts.get_user(user.id)

    assert consent_ok is True
    assert channel_ok is True
    assert refreshed.registration_completed_at is not None
    assert refreshed.consent_accepted_at is not None
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
async def test_plain_text_promo_is_redeemed_automatically(test_services):
    message = DummyMessage(text="  chat100  ", user_id=21011)

    async with test_services.hub() as hub:
        user = await client_handlers.ensure_user(message.from_user, test_services, hub)
        await hub.accounts.complete_registration(user.id)
        await hub.accounts.mark_channel_verified(user.id)
        await hub.accounts.mark_promo_onboarding_completed(user.id)
        await hub.promos.create_code(
            code="CHAT100",
            name="Chat promo",
            reward_kind=PromoRewardKind.BALANCE,
            reward_value=Decimal("100"),
            usage_limit=10,
            expires_at=None,
            new_users_only=False,
            admin_id=None,
        )

    await client_handlers.auto_redeem_plain_text_promo(message, test_services)

    async with test_services.hub() as hub:
        refreshed = await hub.accounts.get_user_by_telegram_id(21011)

    assert refreshed is not None
    assert refreshed.balance_rub == Decimal("100.00")
    assert len(message.answers) == 1
    assert "Промокод применён" in str(message.answers[0]["text"])


@pytest.mark.asyncio
async def test_plain_text_unknown_message_is_silent(test_services):
    message = DummyMessage(text="NOTPROMO", user_id=21012)

    await client_handlers.auto_redeem_plain_text_promo(message, test_services)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(21012)

    assert user is None
    assert message.answers == []


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
async def test_activate_plan_succeeds_when_bundle_loading_fails(monkeypatch):
    captured: dict[str, object] = {}
    media_card = AsyncMock()
    subscription = _paid_subscription_stub()

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
            return subscription

    class DummyAccounts:
        async def get_subscription_bundle(self, user_id):
            raise RuntimeError("panel timeout")

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(billing=DummyBilling(), accounts=DummyAccounts())

    monkeypatch.setattr(client_handlers, "answer_or_edit", fake_answer_or_edit)
    monkeypatch.setattr(client_handlers, "ensure_client_access", fake_ensure_client_access)
    monkeypatch.setattr(client_handlers, "edit_or_send_dynamic_media_card", media_card)

    callback = SimpleNamespace(data=f"client:activate_plan:{PlanCode.UNLIMITED.value}")
    container = SimpleNamespace(hub=fake_hub, settings=SimpleNamespace(backend_public_url=""))

    await client_handlers.activate_plan(callback, container)

    assert "Тариф «Pro» активирован." in str(captured["text"])
    assert "Ссылка для подключения появится в разделе «Моя ссылка»" in str(captured["text"])
    media_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_activate_plan_falls_back_to_text_when_media_card_send_fails(monkeypatch):
    captured: dict[str, object] = {}
    subscription = _paid_subscription_stub()

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
            return subscription

    class DummyAccounts:
        async def get_subscription_bundle(self, user_id):
            return {
                "subscription": subscription,
                "subscription_info": SimpleNamespace(subscriptionUrl="https://sub.example/demo"),
            }

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(billing=DummyBilling(), accounts=DummyAccounts())

    monkeypatch.setattr(client_handlers, "answer_or_edit", fake_answer_or_edit)
    monkeypatch.setattr(client_handlers, "ensure_client_access", fake_ensure_client_access)
    monkeypatch.setattr(
        client_handlers,
        "edit_or_send_dynamic_media_card",
        AsyncMock(side_effect=RuntimeError("telegram media failed")),
    )

    callback = SimpleNamespace(data=f"client:activate_plan:{PlanCode.UNLIMITED.value}")
    container = SimpleNamespace(hub=fake_hub, settings=SimpleNamespace(backend_public_url=""))

    await client_handlers.activate_plan(callback, container)

    assert "Ваша персональная ссылка VPN" in str(captured["text"])
    assert captured["kwargs"]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_subscription_link_handles_bundle_errors_without_crashing(monkeypatch):
    captured: dict[str, object] = {}
    subscription = _paid_subscription_stub()

    async def fake_answer_or_edit(target, text, *, reply_markup=None, **kwargs):
        captured["target"] = target
        captured["text"] = text
        captured["reply_markup"] = reply_markup
        captured["kwargs"] = kwargs
        return None

    async def fake_ensure_client_access(callback, container, hub):
        return SimpleNamespace(id="user-42")

    class DummyAccounts:
        async def get_subscription_bundle(self, user_id):
            raise RuntimeError("panel timeout")

        async def get_current_subscription(self, user_id):
            return subscription

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(accounts=DummyAccounts())

    monkeypatch.setattr(client_handlers, "answer_or_edit", fake_answer_or_edit)
    monkeypatch.setattr(client_handlers, "ensure_client_access", fake_ensure_client_access)

    callback = SimpleNamespace(data="client:subscription_link")
    container = SimpleNamespace(hub=fake_hub, settings=SimpleNamespace(backend_public_url=""))

    await client_handlers.subscription_link(callback, container)

    assert "Ссылка пока недоступна." in str(captured["text"])
    assert "попробуйте открыть этот раздел чуть позже" in str(captured["text"]).lower()


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
    assert "<b>🌍 Тарифы ALTLINK</b>" in text
    assert "<b>Start</b>" in text
    assert "<b>Pro</b>" in text
    assert "🟢" not in text
    assert "🟡" not in text
    assert "До 2 устройств" in text
    assert "До 8 устройств" in text
    assert "⚡ Один случайный высокоскоростной сервер" in text
    assert "серверы со скоростью до 10 Гбит/с" in text
    assert "🛡️ Белые списки отдельно: 2 ₽ за 1 ГБ" in text
    assert "лимит долга -50 ₽" not in text
    assert "🛡️ Поддержка режима белых списков" in text
    assert "⚡ — высокоскоростной сервер" in text
    assert "«БС» — сервер белых списков" in text
    assert "на мобильном интернете работают только отдельные российские сервисы" in text
    assert "ALTLINK помогает вернуть доступ к привычным сервисам!" in text


@pytest.mark.asyncio
async def test_personal_promo_button_applies_code_and_opens_plan_menu(monkeypatch):
    captured: dict[str, object] = {}
    promo = SimpleNamespace(
        code="ALT10-PERSONAL",
        reward_value=Decimal("10"),
    )
    redeem_code = AsyncMock(return_value=(promo, SimpleNamespace(), "activated"))

    async def fake_answer_or_edit(target, text, *, reply_markup=None, **kwargs):
        captured["text"] = text
        captured["reply_markup"] = reply_markup
        captured["kwargs"] = kwargs

    async def fake_ensure_client_access(callback, container, hub):
        return SimpleNamespace(id="user-42")

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(promos=SimpleNamespace(redeem_code=redeem_code))

    monkeypatch.setattr(client_handlers, "answer_or_edit", fake_answer_or_edit)
    monkeypatch.setattr(client_handlers, "ensure_client_access", fake_ensure_client_access)

    callback = SimpleNamespace(data="client:promo_apply:ALT10-PERSONAL")
    container = SimpleNamespace(hub=fake_hub)

    await client_handlers.promo_apply_and_open_plans(callback, container)

    redeem_code.assert_awaited_once_with("user-42", "ALT10-PERSONAL")
    assert "Промокод <code>ALT10-PERSONAL</code> активирован" in str(captured["text"])
    assert "автоматическое продление" in str(captured["text"])
    assert "<b>🌍 Тарифы ALTLINK</b>" in str(captured["text"])
    assert captured["kwargs"]["parse_mode"] == "HTML"


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
        yield SimpleNamespace(
            promos=SimpleNamespace(
                calculate_discount=AsyncMock(return_value=(Decimal("0.00"), None, None))
            )
        )

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
    assert "🛡️ Поддержка режима белых списков" in text
    assert "на мобильном интернете работают только отдельные российские сервисы" in text
    assert "ALTLINK помогает вернуть доступ к привычным сервисам!" in text


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
        yield SimpleNamespace(
            promos=SimpleNamespace(
                calculate_discount=AsyncMock(return_value=(Decimal("0.00"), None, None))
            )
        )

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
    assert "⚡ Один случайный высокоскоростной сервер" in text
    assert "В интерфейсе он отмечен ⚡" in text
    assert "Что такое режим белых списков" in text
    assert "2 ₽ за 1 ГБ" in text
    assert "При балансе -50 ₽" not in text


@pytest.mark.asyncio
async def test_plan_family_menu_shows_discounted_prices_when_promo_is_active(monkeypatch):
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
        yield SimpleNamespace(
            promos=SimpleNamespace(
                calculate_discount=AsyncMock(
                    return_value=(
                        Decimal("19.90"),
                        SimpleNamespace(code="ALT10", reward_value=Decimal("10")),
                        None,
                    )
                )
            )
        )

    monkeypatch.setattr(client_handlers, "answer_or_edit", fake_answer_or_edit)
    monkeypatch.setattr(client_handlers, "ensure_client_access", fake_ensure_client_access)

    callback = SimpleNamespace(data="client:plan_family:unlimited", answer=AsyncMock())
    container = SimpleNamespace(hub=fake_hub)

    await client_handlers.plan_family_menu(callback, container)

    text = str(captured["text"])
    assert "Промокод ALT10 активен" in text
    assert "Скидка 10% уже включена в цены ниже." in text
    assert "На месяц: <s>199 ₽</s> 179.10 ₽" in text
    assert "На неделю: <s>65 ₽</s> 58.50 ₽" in text

    markup = captured["reply_markup"]
    flat = [button.text for row in markup.inline_keyboard for button in row]
    assert "На месяц • 179.10 ₽ (-10%)" in flat
    assert "На неделю • 58.50 ₽ (-10%)" in flat


@pytest.mark.asyncio
async def test_plan_family_menu_falls_back_when_discount_preview_fails(monkeypatch):
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
        yield SimpleNamespace(
            promos=SimpleNamespace(
                calculate_discount=AsyncMock(side_effect=RuntimeError("promo broken"))
            )
        )

    monkeypatch.setattr(client_handlers, "answer_or_edit", fake_answer_or_edit)
    monkeypatch.setattr(client_handlers, "ensure_client_access", fake_ensure_client_access)

    callback = SimpleNamespace(data="client:plan_family:unlimited", answer=AsyncMock())
    container = SimpleNamespace(hub=fake_hub)

    await client_handlers.plan_family_menu(callback, container)

    text = str(captured["text"])
    assert "<b>Pro</b>" in text
    assert "Промокод" not in text


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
async def test_notify_admins_about_support_request_sends_to_admin_bot(monkeypatch):
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
    user = SimpleNamespace(telegram_id=888, username="support_user")

    await client_handlers.notify_admins_about_support_request(
        container,
        user=user,
        request_id="support-55",
        message="Не получается подключиться",
        admin_telegram_ids=[11, 22],
    )

    assert sent[0]["bot_token"] == "admin-token"
    assert sent[0]["chat_ids"] == [11, 22]
    assert "support-55" in str(sent[0]["text"])
    assert "Не получается подключиться" in str(sent[0]["text"])


@pytest.mark.asyncio
async def test_handle_topup_checkout_skips_admin_notifications_for_yookassa(monkeypatch):
    notify_admins = AsyncMock()
    render_checkout = AsyncMock()

    monkeypatch.setattr(client_handlers, "notify_admins_about_topup_request", notify_admins)
    monkeypatch.setattr(client_handlers, "answer_or_edit_topup_checkout", render_checkout)

    checkout = SimpleNamespace(
        provider="yookassa",
        payment_url="https://pay.yookassa.example/confirm/demo",
        request=SimpleNamespace(id="req-yoo-1"),
    )

    await client_handlers.handle_topup_checkout(
        DummyMessage(text="Пополнить", user_id=21012),
        SimpleNamespace(settings=SimpleNamespace()),
        user=SimpleNamespace(id="user-1"),
        amount=Decimal("350"),
        checkout=checkout,
        admin_telegram_ids=[11, 22],
    )

    notify_admins.assert_not_awaited()
    render_checkout.assert_awaited_once()
    assert render_checkout.await_args.kwargs["payment_url"] == "https://pay.yookassa.example/confirm/demo"
    assert render_checkout.await_args.kwargs["can_check"] is True
    assert render_checkout.await_args.kwargs["plan_action_text"] == "🧾 Выбрать тариф"


@pytest.mark.asyncio
async def test_handle_topup_checkout_shows_change_plan_for_paid_subscription(monkeypatch):
    render_checkout = AsyncMock()
    monkeypatch.setattr(client_handlers, "answer_or_edit_topup_checkout", render_checkout)

    checkout = SimpleNamespace(
        provider="yookassa",
        payment_url="https://pay.yookassa.example/confirm/demo",
        request=SimpleNamespace(id="req-yoo-2"),
    )

    await client_handlers.handle_topup_checkout(
        DummyMessage(text="Пополнить", user_id=21013),
        SimpleNamespace(settings=SimpleNamespace()),
        user=SimpleNamespace(id="user-1"),
        amount=Decimal("350"),
        checkout=checkout,
        subscription=SimpleNamespace(plan=SimpleNamespace(is_trial=False)),
    )

    assert render_checkout.await_args.kwargs["plan_action_text"] == "🔄 Сменить тариф"


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
