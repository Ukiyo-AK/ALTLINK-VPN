from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from altlink.domain.enums import ServerType
from altlink.domain.plans import (
    SINGLE_10GBIT_MONTHLY_PRICE_RUB,
    SINGLE_10GBIT_WEEKLY_PRICE_RUB,
    UNLIMITED_MONTHLY_PRICE_RUB,
    UNLIMITED_WEEKLY_PRICE_RUB,
)

USER_OPEN_PREFIX = "adm:uo"
USER_TRIAL_PREFIX = "adm:ut"
USER_ACTIVATE_PREFIX = "adm:ua"
USER_DEACTIVATE_PREFIX = "adm:ud"
USER_PLAN_PREFIX = "adm:up"
USER_BALANCE_PREFIX = "adm:ub"
USER_SUBSCRIPTIONS_PREFIX = "adm:us"
USER_DELETE_PREFIX = "adm:ux"
USER_DELETE_CONFIRM_PREFIX = "adm:xc"

PROMO_TOGGLE_PREFIX = "adm:pt"
PAYMENT_APPROVE_PREFIX = "adm:pa"
PAYMENT_REJECT_PREFIX = "adm:pr"
PAYMENT_REFRESH_PREFIX = "adm:pf"


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Платежи"), KeyboardButton(text="Пользователи")],
            [KeyboardButton(text="Серверы"), KeyboardButton(text="Аналитика")],
            [KeyboardButton(text="Онлайн"), KeyboardButton(text="Топы")],
            [KeyboardButton(text="Запросы поддержки"), KeyboardButton(text="Промокоды")],
            [KeyboardButton(text="Создать промокод"), KeyboardButton(text="Рассылка")],
            [KeyboardButton(text="Логи"), KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите раздел",
    )


def user_actions(user_id: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Подписка и статус", callback_data=f"{USER_SUBSCRIPTIONS_PREFIX}:{user_id}", style="primary")
    builder.button(text="Корректировка баланса", callback_data=f"{USER_BALANCE_PREFIX}:{user_id}", style="primary")
    builder.button(text="Обновить карточку", callback_data=f"{USER_OPEN_PREFIX}:{user_id}")
    builder.button(text="Удалить аккаунт", callback_data=f"{USER_DELETE_PREFIX}:{user_id}", style="danger")
    builder.adjust(2, 2)
    return builder


def user_subscription_actions(user_id: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Тест на 2 дня", callback_data=f"{USER_TRIAL_PREFIX}:{user_id}", style="primary")
    builder.button(text="Активировать", callback_data=f"{USER_ACTIVATE_PREFIX}:{user_id}", style="success")
    builder.button(text="Деактивировать", callback_data=f"{USER_DEACTIVATE_PREFIX}:{user_id}", style="danger")
    builder.button(
        text=f"Start • {SINGLE_10GBIT_MONTHLY_PRICE_RUB} ₽ / месяц",
        callback_data=f"{USER_PLAN_PREFIX}:10m:{user_id}",
        style="primary",
    )
    builder.button(
        text=f"Start • {SINGLE_10GBIT_WEEKLY_PRICE_RUB} ₽ / неделя",
        callback_data=f"{USER_PLAN_PREFIX}:10w:{user_id}",
        style="primary",
    )
    builder.button(
        text=f"Pro • {UNLIMITED_MONTHLY_PRICE_RUB} ₽ / месяц",
        callback_data=f"{USER_PLAN_PREFIX}:unm:{user_id}",
        style="primary",
    )
    builder.button(
        text=f"Pro • {UNLIMITED_WEEKLY_PRICE_RUB} ₽ / неделя",
        callback_data=f"{USER_PLAN_PREFIX}:unw:{user_id}",
        style="primary",
    )
    builder.button(text="Назад к карточке", callback_data=f"{USER_OPEN_PREFIX}:{user_id}")
    builder.adjust(1, 2, 2, 1)
    return builder


def user_delete_confirmation_actions(user_id: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, удалить аккаунт", callback_data=f"{USER_DELETE_CONFIRM_PREFIX}:{user_id}", style="danger")
    builder.button(text="Отмена", callback_data=f"{USER_OPEN_PREFIX}:{user_id}")
    builder.adjust(1, 1)
    return builder


def user_lookup_actions(items) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for item in items:
        label = f"@{item.username}" if item.username else str(item.telegram_id)
        builder.button(
            text=f"{label} • {item.telegram_id}",
            callback_data=f"{USER_OPEN_PREFIX}:{item.id}",
            style="primary",
        )
    builder.adjust(1)
    return builder


def server_actions(server_id: str, is_available: bool) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Убрать из локальной системы" if is_available else "Добавить в локальную систему",
        callback_data=f"admin:server_toggle:{server_id}:{0 if is_available else 1}",
        style="danger" if is_available else "success",
    )
    if server_id == "sync":
        builder.adjust(1)
        return builder
    builder.button(
        text="⚡ Start",
        callback_data=f"admin:server_type:{server_id}:{ServerType.TEN_GBIT.value}",
        style="primary",
    )
    builder.button(
        text="WL",
        callback_data=f"admin:server_type:{server_id}:{ServerType.WHITELIST.value}",
        style="success",
    )
    builder.button(text="Обычный", callback_data=f"admin:server_type:{server_id}:{ServerType.REGULAR.value}")
    builder.adjust(1, 3)
    return builder


def top_users_actions(active_metric: str | None = None) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    metrics = [
        ("traffic", "Трафик"),
        ("whitelist", "Whitelist"),
        ("balance", "Баланс"),
        ("topups", "Пополнения"),
    ]
    for metric, label in metrics:
        kwargs = {"style": "primary"} if metric == active_metric else {}
        builder.button(text=label, callback_data=f"admin:tops:{metric}", **kwargs)
    builder.adjust(2, 2)
    return builder


def support_request_actions(request_id: str, is_resolved: bool) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if not is_resolved:
        builder.button(text="Закрыть запрос", callback_data=f"admin:support:resolve:{request_id}", style="success")
    builder.button(text="Обновить список", callback_data="admin:support:list")
    builder.adjust(1, 1)
    return builder


def payment_request_actions(request_id: str, status: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if status == "new":
        builder.button(text="Подтвердить", callback_data=f"{PAYMENT_APPROVE_PREFIX}:{request_id}", style="success")
        builder.button(text="Отклонить", callback_data=f"{PAYMENT_REJECT_PREFIX}:{request_id}", style="danger")
        builder.adjust(2)
        return builder
    builder.button(text="Обновить", callback_data=f"{PAYMENT_REFRESH_PREFIX}:{request_id}", style="primary")
    builder.adjust(1)
    return builder


def system_logs_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Обновить журнал", callback_data="admin:logs:refresh", style="primary")
    builder.adjust(1)
    return builder


def promo_list_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Создать промокод", callback_data="admin:promo:new", style="success")
    builder.button(text="Обновить список", callback_data="admin:promo:list", style="primary")
    builder.adjust(2)
    return builder


def promo_item_actions(promo_id: str, *, is_active: bool) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Отключить" if is_active else "Включить",
        callback_data=f"{PROMO_TOGGLE_PREFIX}:{promo_id}:{0 if is_active else 1}",
        style="danger" if is_active else "success",
    )
    builder.button(text="К списку", callback_data="admin:promo:list")
    builder.adjust(2)
    return builder


def broadcast_media_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Использовать логотип", callback_data="admin:broadcast:default", style="primary")
    builder.button(text="Отмена", callback_data="admin:broadcast:cancel", style="danger")
    builder.adjust(1, 1)
    return builder


def broadcast_preview_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Отправить всем", callback_data="admin:broadcast:confirm", style="success")
    builder.button(text="Отмена", callback_data="admin:broadcast:cancel", style="danger")
    builder.adjust(1, 1)
    return builder
