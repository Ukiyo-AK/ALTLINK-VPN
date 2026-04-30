from __future__ import annotations

from altlink.domain.plans import (
    SINGLE_10GBIT_MONTHLY_PRICE_RUB,
    SINGLE_10GBIT_WEEKLY_PRICE_RUB,
    UNLIMITED_MONTHLY_PRICE_RUB,
    UNLIMITED_WEEKLY_PRICE_RUB,
)
from altlink.presentation.bots.client_keyboards import (
    agreement_actions,
    balance_actions,
    channel_actions,
    insufficient_balance_actions,
    main_menu,
    menu_actions,
    plan_actions,
    plan_period_actions,
    portal_login_actions,
    portal_login_complete_actions,
    promo_onboarding_actions,
    promo_onboarding_skip_actions,
    subscription_link_actions,
    subscription_actions,
    topup_amount_confirm_actions,
    topup_checkout_actions,
    topup_provider_actions,
)


def keyboard_rows(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.keyboard]


def inline_rows(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def inline_buttons(markup) -> list[dict]:
    return [button.model_dump(exclude_none=True) for row in markup.inline_keyboard for button in row]


def test_main_menu_matches_new_navigation():
    markup = main_menu()
    assert keyboard_rows(markup) == [["Меню", "Профиль"]]
    assert markup.is_persistent is True


def test_menu_actions_include_cabinet_trial_and_styles():
    markup = menu_actions(
        show_trial=True,
        share_url="https://example.com/share",
        portal_url="https://altlink.online/portal/login?token=demo-token",
    ).as_markup()
    rows = inline_rows(markup)
    flat = [text for row in rows for text in row]
    buttons = inline_buttons(markup)
    assert "💳 Баланс" in flat
    assert "🧾 Подписка" in flat
    assert "🌐 Личный кабинет" in flat
    assert "📣 Поделиться VPN" in flat
    assert "🛟 Поддержка" in flat
    assert "🎁 Тест на 2 дня" in flat
    assert rows[1] == ["🌐 Личный кабинет", "📣 Поделиться VPN"]
    assert rows[-1] == ["🎁 Тест на 2 дня"]
    balance_button = next(button for button in buttons if button["text"] == "💳 Баланс")
    portal_button = next(button for button in buttons if button["text"] == "🌐 Личный кабинет")
    share_button = next(button for button in buttons if button["text"] == "📣 Поделиться VPN")
    support_button = next(button for button in buttons if button["text"] == "🛟 Поддержка")
    trial_button = next(button for button in buttons if button["text"] == "🎁 Тест на 2 дня")
    assert balance_button["style"] == "primary"
    assert portal_button["url"] == "https://altlink.online/portal/login?token=demo-token"
    assert share_button["style"] == "success"
    assert support_button["style"] == "primary"
    assert trial_button["style"] == "success"


def test_balance_actions_make_history_primary():
    buttons = inline_buttons(balance_actions().as_markup())
    history_button = next(button for button in buttons if button["text"] == "🧾 История платежей")
    assert history_button["style"] == "primary"


def test_agreement_actions_change_after_confirmation():
    pending = agreement_actions(consent_accepted=False).as_markup()
    pending_flat = [text for row in inline_rows(pending) for text in row]
    assert pending_flat == ["Подтвердить соглашение"]
    assert inline_buttons(pending)[0]["style"] == "success"

    accepted = agreement_actions(consent_accepted=True).as_markup()
    accepted_flat = [text for row in inline_rows(accepted) for text in row]
    assert accepted_flat == ["Соглашение подтверждено", "Меню"]


def test_agreement_actions_include_legal_link_when_available():
    markup = agreement_actions(
        consent_accepted=False,
        agreement_url="https://altlink.online/legal/agreement",
    ).as_markup()
    buttons = inline_buttons(markup)

    assert len(buttons) == 2
    open_button = next(button for button in buttons if button.get("url"))
    confirm_button = next(button for button in buttons if button.get("callback_data") == "client:complete_registration")
    assert open_button["url"] == "https://altlink.online/legal/agreement"
    assert open_button["style"] == "primary"
    assert confirm_button["style"] == "success"


def test_insufficient_balance_actions_lead_to_topup():
    markup = insufficient_balance_actions().as_markup()
    buttons = inline_buttons(markup)
    callbacks = [button["callback_data"] for button in buttons if "callback_data" in button]

    assert len(buttons) == 3
    topup_button = next(button for button in buttons if button.get("callback_data") == "client:topup_menu")
    assert topup_button["callback_data"] == "client:topup_menu"
    assert topup_button["style"] == "success"
    assert "client:plan_menu" in callbacks
    assert "client:balance" in callbacks


def test_channel_actions_include_subscription_check():
    markup = channel_actions("https://t.me/altlink_channel").as_markup()
    flat = [text for row in inline_rows(markup) for text in row]
    buttons = inline_buttons(markup)
    assert "Подписаться на канал" in flat
    assert "Проверить подписку" in flat
    subscribe_button = next(button for button in buttons if button["text"] == "Подписаться на канал")
    check_button = next(button for button in buttons if button["text"] == "Проверить подписку")
    assert subscribe_button["style"] == "primary"
    assert check_button["style"] == "success"


def test_promo_onboarding_actions_offer_enter_and_skip():
    markup = promo_onboarding_actions().as_markup()
    flat = [text for row in inline_rows(markup) for text in row]
    buttons = inline_buttons(markup)
    assert flat == ["Ввести промокод", "Пропустить"]
    assert next(button for button in buttons if button["text"] == "Ввести промокод")["style"] == "success"
    assert next(button for button in buttons if button["text"] == "Пропустить")["style"] == "primary"


def test_promo_onboarding_skip_actions_keep_single_skip_button():
    markup = promo_onboarding_skip_actions().as_markup()
    flat = [text for row in inline_rows(markup) for text in row]
    assert flat == ["Пропустить"]


def test_portal_login_actions_include_confirm_and_cancel():
    markup = portal_login_actions("demo-token").as_markup()
    flat = [text for row in inline_rows(markup) for text in row]
    buttons = inline_buttons(markup)
    assert flat == ["Подтвердить вход", "Отменить"]
    assert next(button for button in buttons if button["text"] == "Подтвердить вход")["style"] == "success"
    assert next(button for button in buttons if button["text"] == "Отменить")["style"] == "danger"


def test_portal_login_complete_actions_include_open_button():
    markup = portal_login_complete_actions("https://altlink.online/portal/login?token=demo-token").as_markup()
    flat = [text for row in inline_rows(markup) for text in row]
    buttons = inline_buttons(markup)
    assert flat == ["🚀 Открыть кабинет", "🏠 Меню"]
    open_button = next(button for button in buttons if button["text"] == "🚀 Открыть кабинет")
    assert open_button["url"] == "https://altlink.online/portal/login?token=demo-token"
    assert open_button["style"] == "success"


def test_subscription_actions_render_cancel_and_hide_traffic():
    metered_flat = [
        text
        for row in inline_rows(
            subscription_actions(show_traffic=True, can_cancel=True, auto_renew_disabled=False).as_markup()
        )
        for text in row
    ]
    assert "Трафик и списания" in metered_flat
    assert "Отказаться от подписки" in metered_flat

    unlimited_flat = [
        text
        for row in inline_rows(
            subscription_actions(show_traffic=False, can_cancel=False, auto_renew_disabled=False).as_markup()
        )
        for text in row
    ]
    assert "Трафик и списания" not in unlimited_flat


def test_subscription_link_actions_offer_one_tap_copy():
    markup = subscription_link_actions(
        show_traffic=True,
        help_url="https://altlink.online/help/connect",
        copy_payload="https://sub.example/demo",
    ).as_markup()
    flat = [text for row in inline_rows(markup) for text in row]
    buttons = inline_buttons(markup)

    assert "📋 Скопировать ссылку" in flat
    copy_button = next(button for button in buttons if button["text"] == "📋 Скопировать ссылку")
    assert copy_button["copy_text"] == {"text": "https://sub.example/demo"}
    assert copy_button["style"] == "success"


def test_plan_actions_switch_to_two_step_flow():
    markup = plan_actions().as_markup()
    rows = inline_rows(markup)
    flat = [text for row in rows for text in row]
    assert rows[0] == ["Start", "Pro"]
    assert "Меню" in flat


def test_plan_period_actions_show_expected_prices():
    ten_gbit_markup = plan_period_actions("10gbit").as_markup()
    ten_gbit_flat = [text for row in inline_rows(ten_gbit_markup) for text in row]
    assert f"На месяц • {SINGLE_10GBIT_MONTHLY_PRICE_RUB} ₽" in ten_gbit_flat
    assert f"На неделю • {SINGLE_10GBIT_WEEKLY_PRICE_RUB} ₽" in ten_gbit_flat
    assert "Назад к тарифам" in ten_gbit_flat

    unlimited_markup = plan_period_actions("unlimited").as_markup()
    unlimited_flat = [text for row in inline_rows(unlimited_markup) for text in row]
    assert f"На месяц • {UNLIMITED_MONTHLY_PRICE_RUB} ₽" in unlimited_flat
    assert f"На неделю • {UNLIMITED_WEEKLY_PRICE_RUB} ₽" in unlimited_flat


def test_topup_amount_confirm_actions_show_pay_path():
    markup = topup_amount_confirm_actions("350.00").as_markup()
    flat = [text for row in inline_rows(markup) for text in row]
    buttons = inline_buttons(markup)

    assert flat == ["💳 Оплатить", "✏️ Изменить сумму", "💳 Баланс"]
    pay_button = next(button for button in buttons if button["text"] == "💳 Оплатить")
    assert pay_button["callback_data"] == "client:topup_provider_menu:350.00"
    assert pay_button["style"] == "success"


def test_topup_provider_actions_render_yookassa_as_direct_link():
    markup = topup_provider_actions(
        "350.00",
        [("yookassa", "💳 YooKassa")],
        provider_urls={"yookassa": "https://pay.example/checkout"},
    ).as_markup()
    flat = [text for row in inline_rows(markup) for text in row]
    buttons = inline_buttons(markup)

    assert flat == ["💳 YooKassa", "⬅️ Назад", "💳 Баланс"]
    provider_button = next(button for button in buttons if button["text"] == "💳 YooKassa")
    back_button = next(button for button in buttons if button["text"] == "⬅️ Назад")
    assert provider_button["url"] == "https://pay.example/checkout"
    assert provider_button["style"] == "success"
    assert back_button["callback_data"] == "client:topup_confirm_amount:350.00"


def test_topup_checkout_actions_can_customize_open_button_label():
    markup = topup_checkout_actions(payment_url="https://t.me/support", payment_label="Открыть поддержку").as_markup()
    buttons = inline_buttons(markup)

    open_button = next(button for button in buttons if button["text"] == "Открыть поддержку")
    assert open_button["url"] == "https://t.me/support"
