from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from altlink.domain.enums import PlanCode


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Заявки"), KeyboardButton(text="Пользователи")],
            [KeyboardButton(text="Серверы"), KeyboardButton(text="Статистика")],
            [KeyboardButton(text="Онлайн"), KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
    )


def topup_actions(request_id: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Подтвердить", callback_data=f"admin:topup_approve:{request_id}")
    builder.button(text="Отклонить", callback_data=f"admin:topup_reject:{request_id}")
    builder.adjust(2)
    return builder


def user_actions(user_id: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Тест", callback_data=f"admin:user_trial:{user_id}")
    builder.button(text="Активировать", callback_data=f"admin:user_activate:{user_id}")
    builder.button(text="Деактивировать", callback_data=f"admin:user_deactivate:{user_id}")
    builder.button(text="Безлимит", callback_data=f"admin:user_plan:{user_id}:{PlanCode.UNLIMITED.value}")
    builder.button(text="50 ГБ", callback_data=f"admin:user_plan:{user_id}:{PlanCode.LIMITED_50GB.value}")
    builder.button(text="Корректировка баланса", callback_data=f"admin:user_balance:{user_id}")
    builder.adjust(2, 2, 1)
    return builder


def server_actions(server_id: str, is_available: bool) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Убрать из локальной системы" if is_available else "Добавить в локальную систему",
        callback_data=f"admin:server_toggle:{server_id}:{0 if is_available else 1}",
    )
    builder.adjust(1)
    return builder
