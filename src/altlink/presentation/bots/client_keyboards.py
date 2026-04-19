from __future__ import annotations

from aiogram.types import InlineKeyboardButton, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from altlink.domain.enums import PlanCode


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Профиль"), KeyboardButton(text="Подписка")],
            [KeyboardButton(text="Баланс"), KeyboardButton(text="Серверы")],
            [KeyboardButton(text="Сайт"), KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
    )


def channel_gate_actions(channel_url: str | None = None) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if channel_url:
        builder.row(InlineKeyboardButton(text="Подписаться на канал", url=channel_url))
    builder.button(text="Проверить подписку", callback_data="client:check_channel")
    builder.adjust(1)
    return builder


def balance_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Пополнить баланс", callback_data="client:topup_menu")
    builder.button(text="История платежей", callback_data="client:my_topups")
    builder.adjust(1)
    return builder


def subscription_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Выбрать тариф", callback_data="client:plan_menu")
    builder.button(text="Запустить тест на 2 дня", callback_data="client:trial_activate")
    builder.button(text="Моя ссылка", callback_data="client:subscription_link")
    builder.button(text="QR-код", callback_data="client:subscription_qr")
    builder.button(text="Трафик и списания", callback_data="client:traffic")
    builder.adjust(1)
    return builder


def plan_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Один сервер 10 Гбит • 69 ₽",
        callback_data=f"client:activate_plan:{PlanCode.SINGLE_10GBIT.value}",
    )
    builder.button(
        text="Безлимит • 200 ₽",
        callback_data=f"client:activate_plan:{PlanCode.UNLIMITED.value}",
    )
    builder.adjust(1)
    return builder


def topup_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for amount in (100, 300, 500, 1000):
        builder.button(text=f"{amount} ₽", callback_data=f"client:topup_amount:{amount}")
    builder.button(text="Своя сумма", callback_data="client:topup_custom")
    builder.adjust(2, 2, 1)
    return builder
