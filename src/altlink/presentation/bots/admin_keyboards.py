from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from altlink.domain.enums import PlanCode, ServerType


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Платежи"), KeyboardButton(text="Пользователи")],
            [KeyboardButton(text="Серверы"), KeyboardButton(text="Аналитика")],
            [KeyboardButton(text="Онлайн"), KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
    )


def user_actions(user_id: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Тест", callback_data=f"admin:user_trial:{user_id}")
    builder.button(text="Активировать", callback_data=f"admin:user_activate:{user_id}")
    builder.button(text="Деактивировать", callback_data=f"admin:user_deactivate:{user_id}")
    builder.button(
        text="10 Гбит • 69 ₽",
        callback_data=f"admin:user_plan:{user_id}:{PlanCode.SINGLE_10GBIT.value}",
    )
    builder.button(
        text="Безлимит • 200 ₽",
        callback_data=f"admin:user_plan:{user_id}:{PlanCode.UNLIMITED.value}",
    )
    builder.button(text="Корректировка баланса", callback_data=f"admin:user_balance:{user_id}")
    builder.adjust(2, 2, 1)
    return builder


def server_actions(server_id: str, is_available: bool) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Убрать из локальной системы" if is_available else "Добавить в локальную систему",
        callback_data=f"admin:server_toggle:{server_id}:{0 if is_available else 1}",
    )
    if server_id == "sync":
        builder.adjust(1)
        return builder
    builder.button(text="⚡ 10 Гбит", callback_data=f"admin:server_type:{server_id}:{ServerType.TEN_GBIT.value}")
    builder.button(text="WL", callback_data=f"admin:server_type:{server_id}:{ServerType.WHITELIST.value}")
    builder.button(text="Обычный", callback_data=f"admin:server_type:{server_id}:{ServerType.REGULAR.value}")
    builder.adjust(1, 3)
    return builder
