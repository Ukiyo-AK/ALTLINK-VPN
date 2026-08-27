from __future__ import annotations

from altlink.domain.enums import PlanCode
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
    device_delete_confirmation_actions,
    device_detail_actions,
    device_list_actions,
    insufficient_balance_actions,
    main_menu,
    menu_actions,
    plan_actions,
    plan_period_actions,
    portal_login_actions,
    portal_login_complete_actions,
    promo_onboarding_actions,
    promo_onboarding_skip_actions,
    referral_actions,
    subscription_link_actions,
    subscription_actions,
    subscription_details_actions,
    subscription_revoke_confirmation_actions,
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
        share_url="https://t.me/share/url?url=https%3A%2F%2Fexample.com%2Fref",
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
    assert share_button["url"] == "https://t.me/share/url?url=https%3A%2F%2Fexample.com%2Fref"
    assert "copy_text" not in share_button
    assert support_button["style"] == "primary"
    assert trial_button["style"] == "success"


def test_menu_actions_can_show_quick_topup_for_users_without_paid_plan():
    markup = menu_actions(show_trial=False, show_quick_topup=True).as_markup()
    rows = inline_rows(markup)
    buttons = inline_buttons(markup)

    assert rows[0] == ["➕ Пополнить баланс"]
    topup_button = next(button for button in buttons if button["text"] == "➕ Пополнить баланс")
    assert topup_button["callback_data"] == "client:topup_menu"
    assert topup_button["style"] == "success"


def test_menu_actions_can_show_quick_plan_for_new_or_trial_users():
    markup = menu_actions(show_trial=False, show_quick_plan=True).as_markup()
    rows = inline_rows(markup)
    buttons = inline_buttons(markup)

    assert rows[0] == ["🧾 Выбрать тариф"]
    plan_button = next(button for button in buttons if button["text"] == "🧾 Выбрать тариф")
    assert plan_button["callback_data"] == "client:plan_menu"
    assert plan_button["style"] == "success"


def test_referral_actions_offer_share_and_clear_navigation():
    share_url = "https://t.me/share/url?url=https%3A%2F%2Ft.me%2FAltlinkbot%3Fstart%3Dref_TEST"
    markup = referral_actions(share_url=share_url).as_markup()
    rows = inline_rows(markup)
    buttons = inline_buttons(markup)

    assert rows == [["📤 Поделиться ссылкой"], ["💳 Баланс", "🏠 Меню"]]
    share_button = next(button for button in buttons if button["text"] == "📤 Поделиться ссылкой")
    assert share_button["url"] == share_url
    assert share_button["style"] == "success"


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


def test_channel_actions_can_include_agreement_link():
    markup = channel_actions(
        "https://t.me/altlink_channel",
        agreement_url="https://altlink.online/legal/agreement",
    ).as_markup()
    buttons = inline_buttons(markup)

    agreement_button = next(button for button in buttons if button["text"] == "📘 Пользовательское соглашение")
    assert agreement_button["url"] == "https://altlink.online/legal/agreement"
    assert agreement_button["style"] == "primary"


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


def test_subscription_actions_render_details_and_hide_traffic():
    metered_flat = [
        text
        for row in inline_rows(
            subscription_actions(show_link=True, show_traffic=True, can_cancel=True, auto_renew_disabled=False).as_markup()
        )
        for text in row
    ]
    assert "Трафик и списания" in metered_flat
    assert "Подробнее" in metered_flat
    assert "Выкл. автопродление" not in metered_flat
    assert "Моя ссылка" in metered_flat

    unlimited_flat = [
        text
        for row in inline_rows(
            subscription_actions(show_link=False, show_traffic=False, can_cancel=False, auto_renew_disabled=False).as_markup()
        )
        for text in row
    ]
    assert "Трафик и списания" not in unlimited_flat
    assert "Моя ссылка" not in unlimited_flat
    assert "Подробнее" not in unlimited_flat


def test_subscription_details_actions_render_auto_renew_and_technical_controls():
    enabled_flat = [
        text
        for row in inline_rows(
            subscription_details_actions(can_manage_auto_renew=True, auto_renew_disabled=False).as_markup()
        )
        for text in row
    ]
    assert "Выкл. автопродление" in enabled_flat
    assert "Вкл. автопродление" not in enabled_flat
    assert "Перевыпустить ссылку" in enabled_flat
    assert "VLESS-ключи" in enabled_flat

    disabled_flat = [
        text
        for row in inline_rows(
            subscription_details_actions(can_manage_auto_renew=True, auto_renew_disabled=True).as_markup()
        )
        for text in row
    ]
    assert "Вкл. автопродление" in disabled_flat
    assert "Выкл. автопродление" not in disabled_flat


def test_subscription_revoke_confirmation_actions_require_explicit_confirmation():
    flat = [
        text
        for row in inline_rows(subscription_revoke_confirmation_actions().as_markup())
        for text in row
    ]
    assert flat == ["Да, перевыпустить", "Отмена"]


def test_subscription_link_actions_keep_only_help_and_navigation():
    markup = subscription_link_actions(
        show_traffic=True,
        help_url="https://altlink.online/help/connect",
    ).as_markup()
    flat = [text for row in inline_rows(markup) for text in row]

    assert "📋 Скопировать ссылку" not in flat
    assert "Помощь по подключению" in flat
    assert "Трафик и списания" in flat
    assert "Подписка" in flat
    assert "Меню" in flat


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

    discounted_markup = plan_period_actions(
        "10gbit",
        monthly_price_text="На месяц • 62.10 ₽ (-10%)",
        weekly_price_text="На неделю • 22.50 ₽ (-10%)",
    ).as_markup()
    discounted_flat = [text for row in inline_rows(discounted_markup) for text in row]
    assert "На месяц • 62.10 ₽ (-10%)" in discounted_flat
    assert "На неделю • 22.50 ₽ (-10%)" in discounted_flat


def test_topup_amount_confirm_actions_show_pay_path():
    markup = topup_amount_confirm_actions("350.00").as_markup()
    flat = [text for row in inline_rows(markup) for text in row]
    buttons = inline_buttons(markup)

    assert flat == ["💳 Оплатить", "✏️ Изменить сумму", "💳 Баланс"]
    pay_button = next(button for button in buttons if button["text"] == "💳 Оплатить")
    assert pay_button["callback_data"] == "client:topup_provider_menu:350.00"
    assert pay_button["style"] == "success"


def test_topup_provider_actions_keep_provider_selection_as_callback():
    markup = topup_provider_actions(
        "350.00",
        [("yookassa", "💳 Юкасса СБП")],
    ).as_markup()
    flat = [text for row in inline_rows(markup) for text in row]
    buttons = inline_buttons(markup)

    assert flat == ["💳 Юкасса СБП", "⬅️ Назад", "💳 Баланс"]
    provider_button = next(button for button in buttons if button["text"] == "💳 Юкасса СБП")
    back_button = next(button for button in buttons if button["text"] == "⬅️ Назад")
    assert provider_button["callback_data"] == "client:topup_provider:yookassa:350.00"
    assert provider_button["style"] == "success"
    assert back_button["callback_data"] == "client:topup_menu"


def test_topup_provider_actions_preserve_selected_plan_token():
    markup = topup_provider_actions(
        "37.50",
        [("yookassa", "💳 Юкасса СБП")],
        selected_plan_token="pm",
    ).as_markup()
    buttons = inline_buttons(markup)

    provider_button = next(button for button in buttons if button["text"] == "💳 Юкасса СБП")
    assert provider_button["callback_data"] == "client:topup_provider:yookassa:37.50:pm"


def test_topup_checkout_actions_can_customize_open_button_label():
    markup = topup_checkout_actions(payment_url="https://t.me/support", payment_label="Открыть поддержку").as_markup()
    buttons = inline_buttons(markup)

    open_button = next(button for button in buttons if button["text"] == "Открыть поддержку")
    assert open_button["url"] == "https://t.me/support"


def test_topup_checkout_actions_can_include_plan_action():
    markup = topup_checkout_actions(
        payment_url="https://pay.example/demo",
        request_id="req-1",
        can_check=True,
        plan_action_text="🔄 Сменить тариф",
    ).as_markup()
    buttons = inline_buttons(markup)

    plan_button = next(button for button in buttons if button["text"] == "🔄 Сменить тариф")
    assert plan_button["callback_data"] == "client:plan_menu"


def test_topup_checkout_actions_preserve_selected_plan_until_payment():
    markup = topup_checkout_actions(
        payment_url="https://pay.example/demo",
        request_id="req-1",
        can_check=True,
        plan_action_text="🧾 Активировать выбранный тариф",
        selected_plan_code=PlanCode.UNLIMITED.value,
        selected_plan_token="pm",
    ).as_markup()
    buttons = inline_buttons(markup)

    check_button = next(button for button in buttons if button["text"] == "🔎 Проверить оплату")
    plan_button = next(
        button for button in buttons if button["text"] == "🧾 Активировать выбранный тариф"
    )
    assert check_button["callback_data"] == "client:topup_check:req-1:pm"
    assert plan_button["callback_data"] == f"client:activate_plan:{PlanCode.UNLIMITED.value}"
    assert plan_button["style"] == "primary"


def test_topup_checkout_actions_preserve_whitelist_package_context():
    markup = topup_checkout_actions(
        payment_url="https://pay.example/demo",
        request_id="req-whitelist",
        can_check=True,
        whitelist_topup=True,
    ).as_markup()
    buttons = inline_buttons(markup)

    check_button = next(button for button in buttons if button["text"] == "🔎 Проверить оплату")
    return_button = next(
        button for button in buttons if button["text"] == "🛡 Вернуться к покупке пакета"
    )
    assert check_button["callback_data"] == "client:topup_check:req-whitelist:wl"
    assert return_button["callback_data"] == "client:whitelist_packages"


def test_device_list_actions_paginate_more_than_eight_devices():
    devices = [{"name": f"Device {index}"} for index in range(10)]

    first_buttons = inline_buttons(device_list_actions(devices, page=0, page_size=6).as_markup())
    second_buttons = inline_buttons(device_list_actions(devices, page=1, page_size=6).as_markup())

    assert len([item for item in first_buttons if item.get("callback_data", "").startswith("client:device:")]) == 6
    assert len([item for item in second_buttons if item.get("callback_data", "").startswith("client:device:")]) == 4
    assert any(item.get("callback_data") == "client:devices:1" for item in first_buttons)
    assert any(item.get("callback_data") == "client:devices:0" for item in second_buttons)


def test_device_delete_callbacks_fit_telegram_limit():
    fingerprint = "123456789abc"
    buttons = inline_buttons(device_detail_actions(page=100, index=1000, fingerprint=fingerprint).as_markup())
    buttons += inline_buttons(device_delete_confirmation_actions(page=100, index=1000, fingerprint=fingerprint).as_markup())

    assert all(len(item["callback_data"].encode("utf-8")) <= 64 for item in buttons)
