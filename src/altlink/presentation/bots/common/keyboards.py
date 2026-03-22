from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def client_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Профиль"), KeyboardButton(text="Подписка")],
            [KeyboardButton(text="Баланс"), KeyboardButton(text="Серверы")],
            [KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
    )


def admin_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Заявки"), KeyboardButton(text="Пользователь")],
            [KeyboardButton(text="Серверы"), KeyboardButton(text="Статистика")],
            [KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
    )


def client_profile_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Мой тариф", callback_data="client:subscription")],
            [InlineKeyboardButton(text="Пополнить баланс", callback_data="client:topup:create")],
            [InlineKeyboardButton(text="Мои заявки", callback_data="client:topup:list")],
            [InlineKeyboardButton(text="Получить тест", callback_data="client:trial")],
        ]
    )


def client_subscription_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Безлимит 200 ₽", callback_data="client:plan:unlimited_30d")],
            [InlineKeyboardButton(text="50 ГБ за 100 ₽", callback_data="client:plan:limited_50gb_30d")],
            [InlineKeyboardButton(text="Показать ссылку", callback_data="client:link")],
        ]
    )


def client_topup_list_inline(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"client:topup:item:{item_id}")]
        for item_id, label in items
    ]
    rows.append([InlineKeyboardButton(text="Создать заявку", callback_data="client:topup:create")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def client_servers_inline(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"client:server:{server_id}")]
        for server_id, label in items
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def client_server_detail_inline(server_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Показать ссылку", callback_data=f"client:serverlink:{server_id}")],
            [InlineKeyboardButton(text="Показать QR", callback_data=f"client:serverqr:{server_id}")],
        ]
    )


def admin_topups_inline(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"admin:topup:{item_id}")]
        for item_id, label in items
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_topup_detail_inline(topup_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить", callback_data=f"admin:topup:approve:{topup_id}")],
            [InlineKeyboardButton(text="Отклонить", callback_data=f"admin:topup:reject:{topup_id}")],
        ]
    )


def admin_user_actions_inline(user_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Выдать тест", callback_data=f"admin:user:trial:{user_id}")],
            [InlineKeyboardButton(text="Активировать безлимит", callback_data=f"admin:user:plan:{user_id}:unlimited_30d")],
            [InlineKeyboardButton(text="Активировать 50 ГБ", callback_data=f"admin:user:plan:{user_id}:limited_50gb_30d")],
            [InlineKeyboardButton(text="Скорректировать баланс", callback_data=f"admin:user:balance:{user_id}")],
            [InlineKeyboardButton(text="Заблокировать", callback_data=f"admin:user:block:{user_id}")],
        ]
    )
