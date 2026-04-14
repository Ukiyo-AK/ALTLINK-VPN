from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from altlink.domain.enums import PlanCode


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Профиль"), KeyboardButton(text="Подписка")],
            [KeyboardButton(text="Баланс"), KeyboardButton(text="Серверы")],
            [KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
    )


def balance_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Пополнить баланс", callback_data="client:topup_menu")
    builder.button(text="Мои заявки", callback_data="client:my_topups")
    builder.adjust(1)
    return builder


def subscription_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Выбрать тариф", callback_data="client:plan_menu")
    builder.button(text="Активировать тест", callback_data="client:trial_activate")
    builder.button(text="Моя ссылка", callback_data="client:subscription_link")
    builder.button(text="QR-код", callback_data="client:subscription_qr")
    builder.button(text="Трафик", callback_data="client:traffic")
    builder.adjust(1)
    return builder


def plan_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Безлимит 200 ₽", callback_data=f"client:activate_plan:{PlanCode.UNLIMITED.value}")
    builder.button(
        text="50 ГБ / 100 ₽",
        callback_data=f"client:activate_plan:{PlanCode.LIMITED_50GB.value}",
    )
    builder.adjust(1)
    return builder


def topup_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for amount in (100, 200, 500, 1000):
        builder.button(text=f"{amount} ₽", callback_data=f"client:topup_amount:{amount}")
    builder.button(text="Своя сумма", callback_data="client:topup_custom")
    builder.adjust(2, 2, 1)
    return builder

