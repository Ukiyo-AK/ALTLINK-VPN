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
USER_MESSAGE_PREFIX = "adm:um"
USER_MESSAGE_CANCEL_PREFIX = "adm:uc"
USER_DELETE_PREFIX = "adm:ux"
USER_DELETE_CONFIRM_PREFIX = "adm:xc"
USER_DEVICES_PREFIX = "adm:dl"
USER_DEVICE_PREFIX = "adm:do"
USERS_SYNC_NODE_ACCESS = "adm:usr:nodes"

PROMO_TOGGLE_PREFIX = "adm:pt"
PAYMENT_APPROVE_PREFIX = "adm:pa"
PAYMENT_REJECT_PREFIX = "adm:pr"
PAYMENT_REFRESH_PREFIX = "adm:pf"
PAYMENT_PAGE_PREFIX = "adm:pg"
DATABASE_BACKUP_OPEN = "admin:db:open"
DATABASE_BACKUP_EXPORT = "admin:db:export"
DATABASE_BACKUP_IMPORT = "admin:db:import"
DATABASE_BACKUP_CONFIRM_IMPORT = "admin:db:confirm"
DATABASE_BACKUP_CANCEL_IMPORT = "admin:db:cancel"
MAINTENANCE_OPEN = "adm:mo"
MAINTENANCE_TOGGLE = "adm:mt"
MAINTENANCE_ADD_EXCEPTION = "adm:ma"
MAINTENANCE_REMOVE_PREFIX = "adm:mr"
MAINTENANCE_PICK_ADD_PREFIX = "adm:mpa"
MAINTENANCE_CANCEL = "adm:mc"
SERVER_OPEN_PREFIX = "adm:so"
SERVER_DELETE_PREFIX = "adm:sd"
SERVER_DELETE_CONFIRM_PREFIX = "adm:sc"
SUPPORT_REPLY_PREFIX = "adm:sr"
SUPPORT_RESOLVE_PREFIX = "adm:ss"
BROADCAST_AUDIENCE_PREFIX = "adm:ba"


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Платежи"), KeyboardButton(text="Пользователи")],
            [KeyboardButton(text="Серверы"), KeyboardButton(text="Аналитика")],
            [KeyboardButton(text="Онлайн"), KeyboardButton(text="Топы")],
            [KeyboardButton(text="Запросы поддержки"), KeyboardButton(text="Промокоды")],
            [KeyboardButton(text="Создать промокод"), KeyboardButton(text="Рассылка")],
            [KeyboardButton(text="Логи"), KeyboardButton(text="База данных")],
            [KeyboardButton(text="Техработы")],
            [KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите раздел",
    )


def user_actions(user_id: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Подписка и статус", callback_data=f"{USER_SUBSCRIPTIONS_PREFIX}:{user_id}", style="primary")
    builder.button(text="Корректировка баланса", callback_data=f"{USER_BALANCE_PREFIX}:{user_id}", style="primary")
    builder.button(text="Написать клиенту", callback_data=f"{USER_MESSAGE_PREFIX}:{user_id}", style="success")
    builder.button(text="Устройства", callback_data=f"{USER_DEVICES_PREFIX}:0:{user_id}", style="primary")
    builder.button(text="Обновить карточку", callback_data=f"{USER_OPEN_PREFIX}:{user_id}")
    builder.button(text="Удалить аккаунт", callback_data=f"{USER_DELETE_PREFIX}:{user_id}", style="danger")
    builder.adjust(2, 2, 2)
    return builder


def user_devices_actions(
    user_id: str,
    devices: list[dict[str, object]],
    *,
    page: int,
    page_size: int,
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    start = page * page_size
    for index, device in enumerate(devices[start : start + page_size], start=start):
        name = str(device.get("name") or "Неизвестное устройство")
        builder.button(
            text=f"📱 {name[:42]}",
            callback_data=f"{USER_DEVICE_PREFIX}:{page}:{index}:{user_id}",
            style="primary",
        )
    total_pages = max((len(devices) + page_size - 1) // page_size, 1)
    if page > 0:
        builder.button(text="← Назад", callback_data=f"{USER_DEVICES_PREFIX}:{page - 1}:{user_id}")
    if page + 1 < total_pages:
        builder.button(text="Вперёд →", callback_data=f"{USER_DEVICES_PREFIX}:{page + 1}:{user_id}")
    builder.button(text="К карточке", callback_data=f"{USER_OPEN_PREFIX}:{user_id}")
    rows = [1] * min(page_size, max(len(devices) - start, 0))
    navigation_count = int(page > 0) + int(page + 1 < total_pages)
    if navigation_count:
        rows.append(navigation_count)
    rows.append(1)
    builder.adjust(*rows)
    return builder


def user_device_detail_actions(user_id: str, *, page: int) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="К устройствам", callback_data=f"{USER_DEVICES_PREFIX}:{page}:{user_id}")
    builder.button(text="К карточке", callback_data=f"{USER_OPEN_PREFIX}:{user_id}")
    builder.adjust(1, 1)
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


def user_lookup_actions(items, *, include_node_sync: bool = False) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for item in items:
        label = f"@{item.username}" if item.username else str(item.telegram_id)
        builder.button(
            text=f"{label} • {item.telegram_id}",
            callback_data=f"{USER_OPEN_PREFIX}:{item.id}",
            style="primary",
        )
    if include_node_sync:
        builder.button(
            text="Синхронизировать доступ к нодам",
            callback_data=USERS_SYNC_NODE_ACCESS,
            style="success",
        )
    builder.adjust(*([1] * (len(items) + int(include_node_sync))))
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
    builder.button(text="Удалить из БД", callback_data=f"{SERVER_DELETE_PREFIX}:{server_id}", style="danger")
    builder.adjust(1, 3, 1)
    return builder


def server_delete_confirmation_actions(server_id: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Да, удалить из базы",
        callback_data=f"{SERVER_DELETE_CONFIRM_PREFIX}:{server_id}",
        style="danger",
    )
    builder.button(text="Отмена", callback_data=f"{SERVER_OPEN_PREFIX}:{server_id}")
    builder.adjust(1, 1)
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


def support_request_actions(
    request_id: str,
    is_resolved: bool,
    *,
    index: int = 0,
    total: int = 1,
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if total > 1:
        previous_index = max(index - 1, 0)
        next_index = min(index + 1, total - 1)
        builder.button(text="◀️", callback_data=f"admin:support:page:{previous_index}")
        builder.button(text=f"{index + 1}/{total}", callback_data="admin:support:list", style="primary")
        builder.button(text="▶️", callback_data=f"admin:support:page:{next_index}")
        builder.adjust(3)
    if not is_resolved:
        builder.button(text="Ответить", callback_data=f"{SUPPORT_REPLY_PREFIX}:{request_id}", style="primary")
        builder.button(text="Закрыть запрос", callback_data=f"{SUPPORT_RESOLVE_PREFIX}:{request_id}", style="success")
    builder.button(text="Обновить список", callback_data="admin:support:list")
    builder.adjust(*( [3] if total > 1 else [] ), *( [2] if not is_resolved else [] ), 1)
    return builder


def payment_request_actions(request_id: str, status: str, *, allow_manual_resolution: bool = True) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if status == "new" and allow_manual_resolution:
        builder.button(text="Подтвердить", callback_data=f"{PAYMENT_APPROVE_PREFIX}:{request_id}", style="success")
        builder.button(text="Отклонить", callback_data=f"{PAYMENT_REJECT_PREFIX}:{request_id}", style="danger")
        builder.adjust(2)
        return builder
    builder.button(text="Обновить", callback_data=f"{PAYMENT_REFRESH_PREFIX}:{request_id}", style="primary")
    builder.adjust(1)
    return builder


def payment_browser_actions(*, request_id: str, status: str, index: int, total: int, allow_manual_resolution: bool = True) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    if total > 1:
        previous_index = max(index - 1, 0)
        next_index = min(index + 1, total - 1)
        builder.button(text="◀️", callback_data=f"{PAYMENT_PAGE_PREFIX}:{previous_index}")
        builder.button(text=f"{index + 1}/{total}", callback_data=f"{PAYMENT_REFRESH_PREFIX}:{request_id}", style="primary")
        builder.button(text="▶️", callback_data=f"{PAYMENT_PAGE_PREFIX}:{next_index}")
        builder.adjust(3)

    if status == "new" and allow_manual_resolution:
        builder.button(text="Подтвердить", callback_data=f"{PAYMENT_APPROVE_PREFIX}:{request_id}", style="success")
        builder.button(text="Отклонить", callback_data=f"{PAYMENT_REJECT_PREFIX}:{request_id}", style="danger")
        builder.adjust(*( [3] if total > 1 else [] ), 2)
        return builder

    builder.button(text="Обновить", callback_data=f"{PAYMENT_REFRESH_PREFIX}:{request_id}", style="primary")
    builder.adjust(*( [3] if total > 1 else [] ), 1)
    return builder


def user_message_prompt_actions(user_id: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад к карточке", callback_data=f"{USER_MESSAGE_CANCEL_PREFIX}:{user_id}")
    builder.adjust(1)
    return builder


def system_logs_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Обновить журнал", callback_data="admin:logs:refresh", style="primary")
    builder.adjust(1)
    return builder


def database_backup_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Экспортировать БД", callback_data=DATABASE_BACKUP_EXPORT, style="success")
    builder.button(text="Импортировать БД", callback_data=DATABASE_BACKUP_IMPORT, style="danger")
    builder.adjust(1, 1)
    return builder


def database_import_prompt_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data=DATABASE_BACKUP_OPEN)
    builder.adjust(1)
    return builder


def database_import_confirmation_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Заменить базу из backup", callback_data=DATABASE_BACKUP_CONFIRM_IMPORT, style="danger")
    builder.button(text="Отмена", callback_data=DATABASE_BACKUP_CANCEL_IMPORT)
    builder.adjust(1, 1)
    return builder


def maintenance_actions(state: dict) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    enabled = bool((state or {}).get("enabled"))
    exceptions = list((state or {}).get("exceptions") or [])
    builder.button(
        text="Выключить техработы" if enabled else "Включить техработы",
        callback_data=MAINTENANCE_TOGGLE,
        style="danger" if enabled else "success",
    )
    builder.button(text="Добавить исключение", callback_data=MAINTENANCE_ADD_EXCEPTION, style="primary")
    builder.button(text="Обновить", callback_data=MAINTENANCE_OPEN)
    for item in exceptions[:12]:
        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            continue
        label = str(item.get("label") or item.get("telegram_id") or user_id)
        builder.button(text=f"Убрать {label}", callback_data=f"{MAINTENANCE_REMOVE_PREFIX}:{user_id}", style="danger")
    if exceptions:
        builder.adjust(1, 2, *([1] * min(len(exceptions), 12)))
    else:
        builder.adjust(1, 2)
    return builder


def maintenance_prompt_actions() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data=MAINTENANCE_CANCEL)
    builder.adjust(1)
    return builder


def maintenance_user_pick_actions(items) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for item in items[:10]:
        label = f"@{item.username}" if item.username else str(item.telegram_id)
        builder.button(
            text=f"{label} • {item.telegram_id}",
            callback_data=f"{MAINTENANCE_PICK_ADD_PREFIX}:{item.id}",
            style="primary",
        )
    builder.button(text="Назад", callback_data=MAINTENANCE_CANCEL)
    builder.adjust(*([1] * (min(len(items), 10) + 1)))
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
    builder.button(text="Без вложения", callback_data="admin:broadcast:text_only")
    builder.button(text="Отмена", callback_data="admin:broadcast:cancel", style="danger")
    builder.adjust(1, 1, 1)
    return builder


def broadcast_preview_actions(selected_audience: str = "all") -> InlineKeyboardBuilder:
    def marker(value: str, label: str) -> str:
        return f"✓ {label}" if selected_audience == value else label

    builder = InlineKeyboardBuilder()
    builder.button(text=marker("all", "Все пользователи"), callback_data=f"{BROADCAST_AUDIENCE_PREFIX}:all")
    builder.button(text=marker("trial", "Только тест"), callback_data=f"{BROADCAST_AUDIENCE_PREFIX}:trial")
    builder.button(text=marker("blocked", "Только заблокированные"), callback_data=f"{BROADCAST_AUDIENCE_PREFIX}:blocked")
    builder.button(text=marker("start", "Все Start"), callback_data=f"{BROADCAST_AUDIENCE_PREFIX}:start")
    builder.button(text=marker("pro", "Все Pro"), callback_data=f"{BROADCAST_AUDIENCE_PREFIX}:pro")
    builder.button(text=marker("single_10gbit", "Start месяц"), callback_data=f"{BROADCAST_AUDIENCE_PREFIX}:single_10gbit")
    builder.button(
        text=marker("single_10gbit_weekly", "Start неделя"),
        callback_data=f"{BROADCAST_AUDIENCE_PREFIX}:single_10gbit_weekly",
    )
    builder.button(text=marker("unlimited", "Pro месяц"), callback_data=f"{BROADCAST_AUDIENCE_PREFIX}:unlimited")
    builder.button(
        text=marker("unlimited_weekly", "Pro неделя"),
        callback_data=f"{BROADCAST_AUDIENCE_PREFIX}:unlimited_weekly",
    )
    builder.button(text="Отправить выбранным", callback_data="admin:broadcast:confirm", style="success")
    builder.button(text="Отмена", callback_data="admin:broadcast:cancel", style="danger")
    builder.adjust(1, 2, 2, 2, 2, 1, 1)
    return builder
