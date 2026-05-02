from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from altlink.domain.enums import PlanCode
from altlink.domain.plans import (
    SINGLE_10GBIT_MONTHLY_PRICE_RUB,
    SINGLE_10GBIT_WEEKLY_PRICE_RUB,
    UNLIMITED_MONTHLY_PRICE_RUB,
    UNLIMITED_WEEKLY_PRICE_RUB,
)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Меню"), KeyboardButton(text="Профиль")]],
        input_field_placeholder="Выберите раздел",
        is_persistent=True,
        resize_keyboard=True,
    )


def channel_actions(channel_url: str | None = None) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if channel_url:
        builder.button(text="Подписаться на канал", url=channel_url, style="primary")
    builder.button(text="Проверить подписку", callback_data="client:check_channel", style="success")
    builder.adjust(2)
    return builder


def agreement_actions(consent_accepted: bool = False, agreement_url: str | None = None) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if agreement_url:
        builder.button(text="📘 Открыть соглашение", url=agreement_url, style="primary")
    builder.button(
        text="Соглашение подтверждено" if consent_accepted else "Подтвердить соглашение",
        callback_data="client:complete_registration",
        style="success",
    )
    if consent_accepted:
        builder.button(text="Меню", callback_data="client:home", style="primary")
        if agreement_url:
            builder.adjust(1, 1, 1)
        else:
            builder.adjust(1, 1)
    else:
        if agreement_url:
            builder.adjust(1, 1)
        else:
            builder.adjust(1)
    return builder


def promo_onboarding_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Ввести промокод", callback_data="client:onboarding_promo_prompt", style="success")
    builder.button(text="Пропустить", callback_data="client:onboarding_promo_skip", style="primary")
    builder.adjust(1, 1)
    return builder


def promo_onboarding_skip_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Пропустить", callback_data="client:onboarding_promo_skip", style="primary")
    builder.adjust(1)
    return builder


def portal_login_actions(token: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Подтвердить вход", callback_data=f"client:portal_login_confirm:{token}", style="success")
    builder.button(text="Отменить", callback_data=f"client:portal_login_cancel:{token}", style="danger")
    builder.adjust(1, 1)
    return builder


def portal_login_complete_actions(portal_url: str | None = None) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if portal_url:
        builder.button(text="🚀 Открыть кабинет", url=portal_url, style="success")
    builder.button(text="🏠 Меню", callback_data="client:home", style="primary")
    builder.adjust(1, 1)
    return builder


def menu_actions(*, show_trial: bool, share_url: str | None = None, portal_url: str | None = None) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Баланс", callback_data="client:balance", style="primary")
    builder.button(text="🧾 Подписка", callback_data="client:subscription", style="primary")
    if portal_url:
        builder.button(text="🌐 Личный кабинет", url=portal_url, style="success")
    if share_url:
        builder.button(text="📣 Поделиться VPN", url=share_url, style="success")
    builder.button(text="🛟 Поддержка", callback_data="client:support", style="primary")
    if show_trial:
        builder.button(text="🎁 Тест на 2 дня", callback_data="client:trial_activate", style="success")
    row_sizes = [2]
    second_row = int(bool(portal_url)) + int(bool(share_url))
    if second_row:
        row_sizes.append(second_row)
    row_sizes.append(1)
    if show_trial:
        row_sizes.append(1)
    builder.adjust(*row_sizes)
    return builder


def balance_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Пополнить баланс", callback_data="client:topup_menu", style="success")
    builder.button(text="🧾 История платежей", callback_data="client:my_topups", style="primary")
    builder.button(text="🎟 Промокод", callback_data="client:promo_prompt", style="primary")
    builder.button(text="👥 Рефералка", callback_data="client:referral", style="success")
    builder.button(text="🏠 Меню", callback_data="client:home")
    builder.adjust(2, 2, 1)
    return builder


def profile_actions(
    *,
    agreement_url: str | None = None,
    privacy_url: str | None = None,
    share_url: str | None = None,
    portal_url: str | None = None,
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if agreement_url:
        builder.button(text="📘 Пользовательское соглашение", url=agreement_url, style="primary")
    if privacy_url:
        builder.button(text="🔐 Политика конфиденциальности", url=privacy_url)
    if portal_url:
        builder.button(text="🌐 Личный кабинет", url=portal_url, style="success")
    if share_url:
        builder.button(text="📣 Поделиться VPN", url=share_url, style="success")
    builder.button(text="🧾 Подписка", callback_data="client:subscription", style="primary")
    builder.button(text="🏠 Меню", callback_data="client:home")
    row_sizes: list[int] = []
    if agreement_url or privacy_url:
        row_sizes.append(2 if agreement_url and privacy_url else 1)
    if portal_url:
        row_sizes.append(1)
    if share_url:
        row_sizes.append(1)
    row_sizes.extend([1, 1])
    builder.adjust(*row_sizes)
    return builder


def subscription_actions(
    *,
    show_traffic: bool,
    can_cancel: bool,
    auto_renew_disabled: bool,
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Выбрать тариф", callback_data="client:plan_menu", style="primary")
    builder.button(text="Моя ссылка", callback_data="client:subscription_link", style="primary")
    if show_traffic:
        builder.button(text="Трафик и списания", callback_data="client:traffic")
    if can_cancel and not auto_renew_disabled:
        builder.button(text="Отказаться от подписки", callback_data="client:subscription_cancel", style="danger")
    if can_cancel and auto_renew_disabled:
        builder.button(text="Включить продление", callback_data="client:subscription_resume", style="success")
    builder.button(text="Меню", callback_data="client:home")
    rows = [2]
    second_row = int(show_traffic) + int(can_cancel)
    if second_row:
        rows.append(second_row)
    rows.append(1)
    builder.adjust(*rows)
    return builder


def subscription_link_actions(
    *,
    show_traffic: bool,
    help_url: str | None = None,
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if help_url:
        builder.button(text="Помощь по подключению", url=help_url, style="primary")
    if show_traffic:
        builder.button(text="Трафик и списания", callback_data="client:traffic")
    builder.button(text="Подписка", callback_data="client:subscription", style="primary")
    builder.button(text="Меню", callback_data="client:home")
    row_sizes: list[int] = []
    if help_url:
        row_sizes.append(1)
    if show_traffic:
        row_sizes.append(1)
    row_sizes.append(2)
    builder.adjust(*row_sizes)
    return builder


def support_actions(support_url: str | None = None) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if support_url:
        builder.button(text="💬 Аккаунт поддержки", url=support_url, style="primary")
    builder.button(text="🛠 Не работает VPN?", callback_data="client:support_issue", style="primary")
    builder.button(text="🏠 Меню", callback_data="client:home")
    builder.adjust(2, 1)
    return builder


def site_actions(site_url: str | None = None, portal_url: str | None = None) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if site_url:
        builder.button(text="🌐 Открыть сайт", url=site_url, style="primary")
    if portal_url:
        builder.button(text="🚀 Личный кабинет", url=portal_url, style="success")
    builder.button(text="🏠 Меню", callback_data="client:home")
    row_sizes: list[int] = []
    links_count = int(bool(site_url)) + int(bool(portal_url))
    if links_count:
        row_sizes.append(links_count)
    row_sizes.append(1)
    builder.adjust(*row_sizes)
    return builder


def plan_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Start", callback_data="client:plan_family:10gbit", style="primary")
    builder.button(text="Pro", callback_data="client:plan_family:unlimited", style="primary")
    builder.button(text="Меню", callback_data="client:home")
    builder.adjust(2, 1)
    return builder


def insufficient_balance_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Пополнить баланс", callback_data="client:topup_menu", style="success")
    builder.button(text="⬅️ К тарифам", callback_data="client:plan_menu", style="primary")
    builder.button(text="💳 Баланс", callback_data="client:balance", style="primary")
    builder.adjust(1, 1, 1)
    return builder


def plan_period_actions(family: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if family == "10gbit":
        builder.button(
            text=f"На месяц • {SINGLE_10GBIT_MONTHLY_PRICE_RUB} ₽",
            callback_data=f"client:activate_plan:{PlanCode.SINGLE_10GBIT.value}",
            style="primary",
        )
        builder.button(
            text=f"На неделю • {SINGLE_10GBIT_WEEKLY_PRICE_RUB} ₽",
            callback_data=f"client:activate_plan:{PlanCode.SINGLE_10GBIT_WEEKLY.value}",
            style="primary",
        )
    else:
        builder.button(
            text=f"На месяц • {UNLIMITED_MONTHLY_PRICE_RUB} ₽",
            callback_data=f"client:activate_plan:{PlanCode.UNLIMITED.value}",
            style="primary",
        )
        builder.button(
            text=f"На неделю • {UNLIMITED_WEEKLY_PRICE_RUB} ₽",
            callback_data=f"client:activate_plan:{PlanCode.UNLIMITED_WEEKLY.value}",
            style="primary",
        )
    builder.button(text="Назад к тарифам", callback_data="client:plan_menu")
    builder.button(text="Меню", callback_data="client:home")
    builder.adjust(1, 1, 1, 1)
    return builder


def topup_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for amount in (100, 300, 500, 1000):
        builder.button(text=f"{amount} ₽", callback_data=f"client:topup_amount:{amount}", style="success")
    builder.button(text="Своя сумма", callback_data="client:topup_custom", style="primary")
    builder.button(text="Меню", callback_data="client:home")
    builder.adjust(2, 2, 1, 1)
    return builder


def topup_amount_confirm_actions(amount_token: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="💳 Оплатить",
        callback_data=f"client:topup_provider_menu:{amount_token}",
        style="success",
    )
    builder.button(text="✏️ Изменить сумму", callback_data="client:topup_menu", style="primary")
    builder.button(text="💳 Баланс", callback_data="client:balance")
    builder.adjust(1, 1, 1)
    return builder


def topup_provider_actions(
    amount_token: str,
    providers: list[tuple[str, str]],
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for provider_code, provider_label in providers:
        builder.button(
            text=provider_label,
            callback_data=f"client:topup_provider:{provider_code}:{amount_token}",
            style="success",
        )
    builder.button(
        text="⬅️ Назад",
        callback_data="client:topup_menu",
        style="primary",
    )
    builder.button(text="💳 Баланс", callback_data="client:balance")
    builder.adjust(*([1] * len(providers)), 1, 1)
    return builder


def topup_checkout_actions(
    *,
    payment_url: str | None = None,
    payment_label: str = "Оплатить",
    request_id: str | None = None,
    can_check: bool = False,
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    rows: list[int] = []
    action_count = 0
    if payment_url:
        builder.button(text=payment_label, url=payment_url, style="success")
        action_count += 1
    if can_check and request_id:
        builder.button(text="🔎 Проверить оплату", callback_data=f"client:topup_check:{request_id}", style="primary")
        action_count += 1
    builder.button(text="🧾 История платежей", callback_data="client:my_topups", style="primary")
    builder.button(text="💳 Баланс", callback_data="client:balance")
    if action_count:
        rows.append(action_count)
    rows.extend([1, 1])
    builder.adjust(*rows)
    return builder
