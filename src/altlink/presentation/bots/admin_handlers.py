from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from altlink.application.services.base import ConflictError, NotFoundError, ServiceError
from altlink.application.services.registry import AppContainer
from altlink.domain.enums import (
    BalanceTransactionType,
    PlanCode,
    PromoRewardKind,
    ServerType,
    SupportRequestStatus,
    SystemEventLevel,
)
from altlink.domain.plans import is_metered_plan_code, parse_paid_plan_code
from altlink.infrastructure.db.models import PromoCode
from altlink.presentation.bots.admin_keyboards import (
    PROMO_TOGGLE_PREFIX,
    PAYMENT_APPROVE_PREFIX,
    PAYMENT_REFRESH_PREFIX,
    PAYMENT_REJECT_PREFIX,
    USER_ACTIVATE_PREFIX,
    USER_BALANCE_PREFIX,
    USER_DEACTIVATE_PREFIX,
    USER_DELETE_CONFIRM_PREFIX,
    USER_DELETE_PREFIX,
    USER_OPEN_PREFIX,
    USER_PLAN_PREFIX,
    USER_SUBSCRIPTIONS_PREFIX,
    USER_TRIAL_PREFIX,
    admin_menu,
    broadcast_media_actions,
    broadcast_preview_actions,
    payment_request_actions,
    promo_list_actions,
    server_actions,
    support_request_actions,
    system_logs_actions,
    top_users_actions,
    user_actions,
    user_delete_confirmation_actions,
    user_lookup_actions,
    user_subscription_actions,
)
from altlink.utils.media import media_path

router = Router(name="admin-router")
logger = logging.getLogger(__name__)

TELEGRAM_USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{4,31}$")
UUIDISH_RE = re.compile(r"^[0-9a-fA-F-]{8,}$")
SHORT_UUID_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")

USER_LOOKUP_PROMPT = "Введите Telegram ID, @username, локальный UUID или Remnawave UUID пользователя."
ADMIN_MENU_TEXTS = {
    "Платежи",
    "Пользователи",
    "Серверы",
    "Аналитика",
    "Онлайн",
    "Топы",
    "Запросы поддержки",
    "Промокоды",
    "Создать промокод",
    "Рассылка",
    "Логи",
    "Помощь",
}
TOP_METRIC_LABELS = {
    "traffic": "общему трафику",
    "whitelist": "трафику по белым спискам",
    "balance": "балансу",
    "topups": "сумме пополнений",
}
SYSTEM_EVENT_LEVEL_LABELS = {
    "info": "Инфо",
    "warning": "Внимание",
    "error": "Ошибка",
}
SHORT_PLAN_CODES = {
    "10m": PlanCode.SINGLE_10GBIT.value,
    "10w": PlanCode.SINGLE_10GBIT_WEEKLY.value,
    "unm": PlanCode.UNLIMITED.value,
    "unw": PlanCode.UNLIMITED_WEEKLY.value,
}

ADMIN_LAST_CARD: dict[int, int] = {}
ADMIN_MODE: dict[int, str] = {}


class BalanceStates(StatesGroup):
    waiting_for_amount = State()


class UserLookupStates(StatesGroup):
    waiting_for_query = State()


class PromoStates(StatesGroup):
    waiting_for_payload = State()


class BroadcastStates(StatesGroup):
    waiting_for_text = State()
    waiting_for_media = State()


async def is_admin(telegram_id: int, container: AppContainer) -> bool:
    async with container.hub() as hub:
        return await hub.accounts.can_access_admin_bot(telegram_id)


def remember_admin_card(message: Message) -> None:
    if message is None:
        return
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        from_user = getattr(message, "from_user", None)
        chat_id = getattr(from_user, "id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        return
    ADMIN_LAST_CARD[chat_id] = message_id


def clear_admin_mode(telegram_id: int) -> None:
    ADMIN_MODE.pop(telegram_id, None)


def set_admin_mode(telegram_id: int, mode: str) -> None:
    ADMIN_MODE[telegram_id] = mode


def admin_mode(telegram_id: int) -> str | None:
    return ADMIN_MODE.get(telegram_id)


async def render_admin(
    target: Message | CallbackQuery,
    text: str,
    *,
    reply_markup=None,
    force_new: bool = False,
) -> Message:
    anchor = target.message if isinstance(target, CallbackQuery) else target
    if not force_new and isinstance(target, CallbackQuery):
        try:
            result = await target.message.edit_text(text, reply_markup=reply_markup)
            remember_admin_card(target.message)
            await target.answer()
            return result
        except TelegramBadRequest:
            pass

    result = await anchor.answer(text, reply_markup=reply_markup)
    remember_admin_card(result)
    if isinstance(target, CallbackQuery):
        await target.answer()
    return result


def compact_text(value: object, limit: int = 96) -> str:
    text = str(value).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 1)].rstrip() + "..."


def billing_cycle_label(plan) -> str:
    if plan is None:
        return "—"
    return "еженедельно" if plan.period_days <= 7 else "ежемесячно"


def user_label(user) -> str:
    if user is None:
        return "n/a"
    if user.username:
        return f"@{user.username}"
    return str(user.telegram_id)


def format_top_users(metric: str, rows) -> str:
    label = TOP_METRIC_LABELS[metric]
    if not rows:
        return f"Топ пользователей по {label}\n\nДанных пока нет."

    lines = [f"Топ пользователей по {label}", ""]
    for index, row in enumerate(rows, start=1):
        if metric in {"traffic", "whitelist"}:
            value = f"{int(row.value) / 1024**3:.2f} ГБ"
        else:
            value = f"{Decimal(row.value):.2f} ₽"
        lines.append(f"{index}. {user_label(row.user)} — {value}")
    return "\n".join(lines)


def format_event_payload(payload: dict | None) -> str | None:
    if not payload:
        return None
    parts = []
    for index, (key, value) in enumerate(payload.items()):
        if index >= 4:
            break
        parts.append(f"{key}={compact_text(value, 42)}")
    return ", ".join(parts) if parts else None


def format_system_events(events) -> str:
    if not events:
        return "Системный журнал\n\nСобытий пока нет."

    lines = ["Системный журнал", "", "Последние события приложения:"]
    for event in events:
        level = SYSTEM_EVENT_LEVEL_LABELS.get(str(event.level), str(event.level).upper())
        lines.append(f"{event.created_at:%d.%m %H:%M} • {level}")
        lines.append(f"{event.source} / {event.event_type}")
        lines.append(compact_text(event.message, 180))
        details = format_event_payload(event.payload)
        if details:
            lines.append(f"Детали: {details}")
        lines.append("")
    return "\n".join(lines).strip()


def format_system_log_screen(status: dict, events) -> str:
    lines = format_panel_status(status)
    lines.extend(["", format_system_events(events)])
    return "\n".join(lines)


def format_activity_summary(summary: dict | None) -> list[str]:
    if not summary:
        return ["Онлайн: данных пока нет"]

    lines = [
        f"Онлайн: {summary.get('current_status', 'неизвестно')}",
        f"Текущее устройство: {summary.get('current_device', 'не определено')}",
        f"Уникальных устройств: {summary.get('unique_device_count', 0)}",
        f"Уникальных IP: {summary.get('unique_ip_count', 0)}",
    ]
    if summary.get("last_ip"):
        lines.append(f"Последний IP: {summary['last_ip']}")
    if summary.get("last_seen_at"):
        lines.append(f"Последняя активность: {summary['last_seen_at']}")
    recent_devices = summary.get("recent_devices") or []
    if recent_devices:
        device_line = ", ".join(
            compact_text(f"{item.get('label')} ({item.get('hits')}x)", 36)
            for item in recent_devices[:4]
        )
        lines.append(f"Недавние устройства: {device_line}")
    recent_ips = summary.get("recent_ips") or []
    if recent_ips:
        ip_line = ", ".join(compact_text(f"{item.get('ip')} ({item.get('hits')}x)", 28) for item in recent_ips[:4])
        lines.append(f"Недавние IP: {ip_line}")
    return lines


def format_support_request(item) -> str:
    user = item.user
    status = "закрыт" if item.status == SupportRequestStatus.RESOLVED else "новый"
    lines = [
        "Запрос поддержки",
        "",
        f"Номер: {item.id}",
        f"Пользователь: {user_label(user)}",
        f"Telegram ID: {user.telegram_id if user else 'n/a'}",
        f"Статус: {status}",
        f"Создан: {item.created_at:%d.%m.%Y %H:%M}",
        f"Тема: {item.topic}",
        "",
        item.message,
    ]
    if item.resolution_comment:
        lines.extend(["", f"Комментарий закрытия: {item.resolution_comment}"])
    if item.resolved_at:
        lines.append(f"Закрыт: {item.resolved_at:%d.%m.%Y %H:%M}")
    return "\n".join(lines)


def format_payment_request(item) -> str:
    status_label = {
        "new": "ожидает решения",
        "approved": "подтверждён",
        "rejected": "отклонён",
        "canceled": "отменён пользователем",
    }.get(str(item.status), str(item.status))
    lines = [
        "Заявка на пополнение",
        "",
        f"Номер: {item.id}",
        f"Пользователь: {user_label(item.user)}",
        f"Telegram ID: {item.user.telegram_id if item.user else 'n/a'}",
        f"Сумма: {Decimal(item.amount_rub):.2f} ₽",
        f"Статус: {status_label}",
        f"Создан: {item.created_at:%d.%m.%Y %H:%M}",
    ]
    if item.user_comment:
        lines.extend(["", f"Комментарий пользователя: {item.user_comment}"])
    if item.admin_comment:
        lines.extend(["", f"Комментарий администратора: {item.admin_comment}"])
    return "\n".join(lines)


def format_panel_status(status: dict) -> list[str]:
    remnawave_label = "OK" if status.get("remnawave_ok") else "ошибка"
    return [
        "Состояние панели",
        f"Remnawave: {remnawave_label}",
        f"База данных: {status.get('database_dialect', 'unknown')}",
        f"Размер БД: {status.get('db_size_gb', 0)} ГБ",
    ]


def format_server_card(server) -> str:
    type_label = {
        ServerType.TEN_GBIT: "Start",
        ServerType.WHITELIST: "Whitelist",
        ServerType.REGULAR: "Обычный",
    }.get(getattr(server, "server_type", ServerType.REGULAR), "Обычный")
    return (
        f"{getattr(server, 'name', 'Сервер')}\n"
        f"Адрес: {getattr(server, 'address', '—')}\n"
        f"Тип: {type_label}\n"
        f"Локально: {'включён' if getattr(server, 'is_available', False) else 'выключен'}\n"
        f"Статус: {'online' if getattr(server, 'is_connected', False) else 'offline'}\n"
        f"Клиенты: {getattr(server, 'current_clients', 0)}/{getattr(server, 'max_clients', None) or 'n/a'}\n"
        f"Нагрузка: {getattr(server, 'load_percent', 0)}%"
    )


def parse_user_plan(data: str) -> tuple[str, PlanCode | None]:
    _, _, short_code, user_id = data.split(":", 3)
    return user_id, parse_paid_plan_code(SHORT_PLAN_CODES.get(short_code))


def normalize_user_lookup_query(text: str | None) -> str:
    normalized = (text or "").strip()
    if not normalized:
        return ""
    lowered = normalized.casefold()
    if lowered.startswith("https://t.me/"):
        normalized = normalized.split("/", 3)[-1].strip()
    elif lowered.startswith("http://t.me/"):
        normalized = normalized.split("/", 3)[-1].strip()
    elif lowered.startswith("t.me/"):
        normalized = normalized.split("/", 1)[-1].strip()
    elif lowered.startswith("tg://user?id="):
        normalized = normalized.split("=", 1)[-1].strip()
    return normalized.removeprefix("@").strip() if normalized.startswith("@@") else normalized.strip()


def is_user_lookup_query(text: str | None) -> bool:
    normalized = normalize_user_lookup_query(text)
    if not normalized or normalized in ADMIN_MENU_TEXTS:
        return False
    if normalized.lstrip("-").isdigit():
        return True
    if TELEGRAM_USERNAME_RE.fullmatch(normalized):
        return True
    if UUIDISH_RE.fullmatch(normalized):
        return True
    if normalized.startswith("sub-") and SHORT_UUID_RE.fullmatch(normalized.removeprefix("sub-")):
        return True
    return False


async def route_admin_menu_action(message: Message, state: FSMContext, container: AppContainer) -> bool:
    text = (message.text or "").strip()
    if text not in ADMIN_MENU_TEXTS:
        return False

    await state.clear()
    clear_admin_mode(message.from_user.id)
    if text == "Платежи":
        await payments_screen(message, container)
    elif text == "Пользователи":
        await users_prompt(message, container, state)
    elif text == "Серверы":
        await servers_screen(message, container)
    elif text == "Аналитика":
        await statistics(message, container)
    elif text == "Онлайн":
        await online(message, container)
    elif text == "Топы":
        await top_users_menu(message, container)
    elif text == "Запросы поддержки":
        await support_requests_screen(message, container)
    elif text == "Промокоды":
        await promo_codes_screen(message, container)
    elif text == "Создать промокод":
        await promo_create_reply(message, state, container)
    elif text == "Рассылка":
        await broadcast_prompt(message, state, container)
    elif text == "Логи":
        await system_logs_screen(message, container)
    else:
        await admin_help(message, container)
    return True


async def show_user_card(target: Message | CallbackQuery, user_id: str, container: AppContainer):
    async with container.hub() as hub:
        card = await hub.accounts.user_card(user_id)
        user = card["user"]
        subscription = card["subscription"]
        get_latest = getattr(hub.accounts, "get_latest_subscription", None)
        latest_subscription = await get_latest(user.id) if subscription is None and get_latest is not None else subscription
        activity_summary = await hub.online.get_user_activity_summary(user.id) if getattr(hub, "online", None) else None
        registered = hub.accounts.is_registered(user)
        topups = card.get("topups", [])
        transactions = card.get("transactions", [])

    if not hasattr(user, "id"):
        user.id = user_id
    if not hasattr(user, "assigned_server"):
        user.assigned_server = None
    if not hasattr(user, "remnawave_user_uuid"):
        user.remnawave_user_uuid = None
    if not hasattr(user, "remnawave_short_uuid"):
        user.remnawave_short_uuid = None
    if not hasattr(user, "referral_code"):
        user.referral_code = None

    plan = subscription.plan if subscription else getattr(latest_subscription, "plan", None)
    plan_name = plan.name if plan else "не выбран"
    next_billing = subscription.next_billing_at.strftime("%d.%m.%Y %H:%M") if subscription else "—"
    assigned_server = user.assigned_server.name if user.assigned_server else "не назначен"
    lines = [
        "Карточка пользователя",
        "",
        f"Пользователь: {user_label(user)}",
        f"Telegram ID: {user.telegram_id}",
        f"Локальный UUID: {user.id}",
        f"Remnawave UUID: {user.remnawave_user_uuid or '—'}",
        f"Remnawave short UUID: {user.remnawave_short_uuid or '—'}",
        f"Регистрация: {'завершена' if registered else 'ожидает подтверждения'}",
        f"Статус: {user.status}",
        f"Баланс: {Decimal(user.balance_rub):.2f} ₽",
        f"Реферальный код: {user.referral_code or '—'}",
        f"Тариф: {plan_name}",
        f"Формат списания: {billing_cycle_label(plan)}",
        f"Следующее списание: {next_billing}",
        f"Автопродление: {'включено' if subscription and subscription.auto_renew else 'отключено'}",
        f"Лимит устройств: {plan.device_limit if plan else '—'}",
        f"Назначенный сервер: {assigned_server}",
        f"Пополнений: {len(topups)}",
        f"Транзакций: {len(transactions)}",
    ]
    if subscription:
        lines.append(f"Трафик: {subscription.traffic_used_bytes / 1024**3:.2f} ГБ")
        if is_metered_plan_code(subscription.plan.code):
            lines.append(f"Белые списки: {subscription.whitelist_traffic_used_bytes / 1024**3:.2f} ГБ")
        if subscription.notes:
            lines.extend(["", f"Примечание: {subscription.notes}"])
    lines.extend([""] + format_activity_summary(activity_summary))
    text = "\n".join(lines)
    try:
        await render_admin(target, text, reply_markup=user_actions(user_id).as_markup())
    except TelegramBadRequest:
        logger.warning("Failed to render user card markup for user_id=%s, sending plain text fallback.", user_id)
        await render_admin(target, text)


async def show_subscription_controls(target: Message | CallbackQuery, user_id: str, container: AppContainer):
    async with container.hub() as hub:
        card = await hub.accounts.user_card(user_id)
        user = card["user"]
        subscription = card["subscription"]

    text = (
        "Управление подпиской\n\n"
        f"Пользователь: {user_label(user)}\n"
        f"Текущий тариф: {subscription.plan.name if subscription else 'нет активного тарифа'}\n"
        f"Следующее списание: {subscription.next_billing_at if subscription else '—'}\n"
        f"Автопродление: {'включено' if subscription and subscription.auto_renew else 'отключено'}\n\n"
        "Здесь можно выдать тест, вручную активировать или деактивировать доступ и выбрать нужный тариф."
    )
    await render_admin(target, text, reply_markup=user_subscription_actions(user_id).as_markup())


async def show_delete_warning(target: Message | CallbackQuery, user_id: str, container: AppContainer):
    async with container.hub() as hub:
        user = await hub.accounts.get_user(user_id)
    text = (
        "Удаление аккаунта\n\n"
        f"Пользователь: {user_label(user)}\n"
        f"Telegram ID: {user.telegram_id}\n\n"
        "Внимание: действие необратимо. Будут удалены локальные данные пользователя, подписки, платежи и связанные записи."
    )
    await render_admin(target, text, reply_markup=user_delete_confirmation_actions(user_id).as_markup())


async def users_prompt(target: Message | CallbackQuery, container: AppContainer, state: FSMContext | None = None):
    telegram_id = target.from_user.id if isinstance(target, CallbackQuery) else target.from_user.id
    set_admin_mode(telegram_id, "user_search")
    if state is not None:
        await state.set_state(UserLookupStates.waiting_for_query)
    async with container.hub() as hub:
        recent_users = await hub.accounts.list_users()
    reply_markup = user_lookup_actions(recent_users[:8]).as_markup() if recent_users else None
    text = USER_LOOKUP_PROMPT
    if recent_users:
        text += "\n\nМожно сразу открыть одного из последних пользователей кнопками ниже."
    await render_admin(target, text, reply_markup=reply_markup)


async def process_user_lookup(target: Message | CallbackQuery, container: AppContainer) -> bool:
    query = normalize_user_lookup_query(target.text if isinstance(target, Message) else "")
    if not query:
        await render_admin(target, USER_LOOKUP_PROMPT)
        return False

    logger.info("Admin user lookup query=%s from telegram_id=%s", query, target.from_user.id)
    try:
        async with container.hub() as hub:
            users = await hub.accounts.list_users(query)
        logger.info("Admin user lookup result_count=%s for query=%s", len(users), query)

        if not users:
            await render_admin(
                target,
                "Пользователь не найден.\n\nПопробуйте другой Telegram ID, @username, локальный UUID или Remnawave UUID.",
            )
            return False

        if len(users) == 1:
            await show_user_card(target, users[0].id, container)
            return True

        text_lines = [f"Найдено пользователей: {len(users)}.", "", "Выберите нужного:"]
        for item in users[:10]:
            text_lines.append(f"• {user_label(item)} — {item.telegram_id}")
        await render_admin(
            target,
            "\n".join(text_lines),
            reply_markup=user_lookup_actions(users[:10]).as_markup(),
        )
        return True
    except Exception:
        logger.exception("Admin user lookup failed for query=%s", query)
        await render_admin(
            target,
            "Не удалось открыть карточку пользователя.\n\nПопробуйте ещё раз или посмотрите раздел «Логи».",
        )
        return False


def build_promo_list_markup(items) -> object:
    builder = InlineKeyboardBuilder()
    builder.button(text="Создать промокод", callback_data="admin:promo:new", style="success")
    builder.button(text="Обновить список", callback_data="admin:promo:list", style="primary")
    for item in items[:8]:
        status = "ON" if item.is_active else "OFF"
        builder.button(text=f"{item.code} • {status}", callback_data=f"{PROMO_TOGGLE_PREFIX}:{item.id}:{0 if item.is_active else 1}")
    builder.adjust(2, *([1] * min(len(items[:8]), 8)))
    return builder.as_markup()


def format_promo_list(items) -> str:
    lines = ["Промокоды", ""]
    if not items:
        lines.append("Пока нет созданных промокодов.")
        lines.append("")
        lines.append("Нажмите кнопку ниже, чтобы создать первый.")
        return "\n".join(lines)

    for item in items[:12]:
        reward_label = (
            f"баланс +{Decimal(item.reward_value):.2f} ₽"
            if item.reward_kind == PromoRewardKind.BALANCE
            else f"скидка {Decimal(item.reward_value):.2f}%"
        )
        uses_label = (
            f"{item.used_count}/{item.usage_limit}"
            if item.usage_limit is not None
            else f"{item.used_count}/∞"
        )
        expiry = item.expires_at.strftime("%d.%m.%Y %H:%M") if item.expires_at else "без срока"
        lines.append(
            f"{item.code} — {'активен' if item.is_active else 'отключён'}\n"
            f"Награда: {reward_label}\n"
            f"Использования: {uses_label}\n"
            f"Новые пользователи: {'да' if item.new_users_only else 'нет'}\n"
            f"Срок: {expiry}"
        )
        lines.append("")
    lines.append("Для создания отправьте параметры отдельным сообщением после кнопки «Создать промокод».")
    return "\n".join(lines).strip()


def promo_create_help_text() -> str:
    return (
        "Создание промокода\n\n"
        "Отправьте параметры одним сообщением, по одному в строке:\n"
        "CODE=START100\n"
        "NAME=Стартовый бонус\n"
        "TYPE=balance или discount\n"
        "VALUE=100\n"
        "USES=50 или *\n"
        "EXPIRES=2026-05-31 23:59 или *\n"
        "NEW_ONLY=yes или no"
    )


def parse_promo_payload(raw_text: str) -> dict:
    values: dict[str, str] = {}
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            raise ConflictError("Не удалось разобрать строку. Используйте формат KEY=VALUE.")
        values[key.strip().lower().replace(" ", "_")] = value.strip()

    code = values.get("code")
    name = values.get("name") or code
    type_raw = (values.get("type") or values.get("reward") or "").strip().lower()
    if type_raw in {"balance", "topup", "deposit"}:
        reward_kind = PromoRewardKind.BALANCE
    elif type_raw in {"discount", "plan_discount", "tariff_discount"}:
        reward_kind = PromoRewardKind.PLAN_DISCOUNT
    else:
        raise ConflictError("TYPE должен быть balance или discount.")

    value_raw = values.get("value")
    if value_raw is None:
        raise ConflictError("Укажите VALUE.")
    try:
        reward_value = Decimal(value_raw.replace(",", "."))
    except InvalidOperation as exc:
        raise ConflictError("VALUE должен быть числом.") from exc

    uses_raw = values.get("uses") or values.get("usage_limit") or "*"
    usage_limit = None if uses_raw in {"*", "∞", "inf"} else int(uses_raw)

    expires_raw = values.get("expires") or values.get("expires_at") or "*"
    expires_at = None
    if expires_raw not in {"*", "∞", "inf"}:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(expires_raw, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ConflictError("EXPIRES должен быть в формате YYYY-MM-DD или YYYY-MM-DD HH:MM.")
        expires_at = parsed.replace(tzinfo=UTC)

    new_only_raw = (values.get("new_only") or values.get("new_users_only") or "no").strip().lower()
    new_users_only = new_only_raw in {"yes", "y", "true", "1", "да"}

    if not code:
        raise ConflictError("Укажите CODE.")

    return {
        "code": code,
        "name": name or code,
        "reward_kind": reward_kind,
        "reward_value": reward_value,
        "usage_limit": usage_limit,
        "expires_at": expires_at,
        "new_users_only": new_users_only,
    }


def broadcast_logo_path() -> Path | None:
    return media_path("logo with title.png") or media_path("logo.png")


def broadcast_failure_reason(exc: Exception) -> str:
    message = str(exc).casefold()
    if "chat not found" in message:
        return "чат с клиентским ботом не открыт"
    if "blocked by the user" in message:
        return "клиент заблокировал бота"
    if "bot was kicked" in message:
        return "бот удалён из чата"
    if "user is deactivated" in message:
        return "аккаунт Telegram деактивирован"
    if "timeout" in message or "timed out" in message:
        return "таймаут Telegram API"
    if "network" in message or "connectorerror" in message or "server disconnected" in message:
        return "сетевая ошибка Telegram API"
    return compact_text(exc, 72)


async def load_broadcast_media_bytes(callback: CallbackQuery, file_id: str | None) -> bytes | None:
    if not file_id:
        return None
    buffer = BytesIO()
    await callback.bot.download(file_id, destination=buffer)
    return buffer.getvalue()


async def show_broadcast_preview(
    target: Message | CallbackQuery,
    state: FSMContext,
    *,
    use_default_logo: bool,
) -> None:
    data = await state.get_data()
    text = (data.get("broadcast_text") or "").strip()
    file_id = data.get("broadcast_file_id")
    preview_text = (
        "Предпросмотр рассылки\n\n"
        f"{text}\n\n"
        f"Изображение: {'кастомное' if file_id else 'логотип по умолчанию' if use_default_logo else 'нет'}"
    )
    if file_id or use_default_logo:
        anchor = target.message if isinstance(target, CallbackQuery) else target
        if file_id:
            await anchor.answer_photo(photo=file_id, caption=preview_text, reply_markup=broadcast_preview_actions().as_markup())
        else:
            logo = broadcast_logo_path()
            if logo is not None:
                await anchor.answer_photo(
                    photo=FSInputFile(str(logo)),
                    caption=preview_text,
                    reply_markup=broadcast_preview_actions().as_markup(),
                )
            else:
                await render_admin(target, preview_text, reply_markup=broadcast_preview_actions().as_markup(), force_new=True)
    else:
        await render_admin(target, preview_text, reply_markup=broadcast_preview_actions().as_markup(), force_new=True)


@router.message(CommandStart())
async def start(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        await message.answer("Доступ к admin bot запрещён.")
        return
    clear_admin_mode(message.from_user.id)
    await message.answer(
        "admin altlink bot готов к работе.\n\n"
        "Здесь можно искать пользователей, смотреть их подписки, баланс, online-сессии, логи, серверы, промокоды и рассылки.",
        reply_markup=admin_menu(),
    )


@router.message(F.text == "Помощь")
async def admin_help(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    await render_admin(
        message,
        "Помощь по admin bot\n\n"
        "Пользователи: поиск по Telegram ID, @username, локальному UUID и Remnawave UUID.\n"
        "Карточка пользователя: подписка, баланс, online, удаление аккаунта.\n"
        "Промокоды: создание и быстрый контроль активности.\n"
        "Рассылка: сообщение всем пользователям с вашей картинкой или логотипом сервиса.\n"
        "Логи: последние системные события приложения.\n"
        "Если нужно быстро найти пользователя, просто нажмите «Пользователи» и отправьте ID, username или UUID.",
    )


@router.message(F.text == "Платежи")
async def payments_screen(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        items = await hub.topups.list_requests()
    if not items:
        await render_admin(message, "Последние платежи\n\nПлатежей пока нет.")
        return
    pending_count = len([item for item in items if str(item.status) == "new"])
    approved_count = len([item for item in items if str(item.status) == "approved"])
    rejected_count = len([item for item in items if str(item.status) == "rejected"])
    await render_admin(
        message,
        "Платежи\n\n"
        f"Ожидают решения: {pending_count}\n"
        f"Подтверждены: {approved_count}\n"
        f"Отклонены: {rejected_count}\n\n"
        "Ниже показаны последние заявки на пополнение.",
    )
    for item in items[:8]:
        sent = await message.answer(
            format_payment_request(item),
            reply_markup=payment_request_actions(item.id, str(item.status)).as_markup(),
        )
        remember_admin_card(sent)


@router.callback_query(F.data.startswith(f"{PAYMENT_REFRESH_PREFIX}:"))
async def payment_refresh(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    request_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        item = await hub.topups.get_request(request_id)
    await render_admin(
        callback,
        format_payment_request(item),
        reply_markup=payment_request_actions(item.id, str(item.status)).as_markup(),
    )


@router.callback_query(F.data.startswith(f"{PAYMENT_APPROVE_PREFIX}:"))
async def payment_approve(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    request_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(callback.from_user.id)
        item = await hub.topups.approve(request_id, admin_id=admin.id if admin else None, comment="Подтверждено в admin bot")
    await render_admin(
        callback,
        format_payment_request(item),
        reply_markup=payment_request_actions(item.id, str(item.status)).as_markup(),
    )


@router.callback_query(F.data.startswith(f"{PAYMENT_REJECT_PREFIX}:"))
async def payment_reject(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    request_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(callback.from_user.id)
        item = await hub.topups.reject(request_id, admin_id=admin.id if admin else None, comment="Отклонено в admin bot")
    await render_admin(
        callback,
        format_payment_request(item),
        reply_markup=payment_request_actions(item.id, str(item.status)).as_markup(),
    )


@router.message(F.text == "Пользователи")
async def users_screen(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    await users_prompt(message, container, state)


@router.callback_query(F.data.startswith(f"{USER_OPEN_PREFIX}:"))
async def open_user(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await show_user_card(callback, callback.data.split(":")[-1], container)


@router.callback_query(F.data.startswith(f"{USER_SUBSCRIPTIONS_PREFIX}:"))
async def open_user_subscription_actions(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await show_subscription_controls(callback, callback.data.split(":")[-1], container)


@router.callback_query(F.data.startswith(f"{USER_DELETE_PREFIX}:"))
async def open_user_delete_warning(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await show_delete_warning(callback, callback.data.split(":")[-1], container)


@router.callback_query(F.data.startswith(f"{USER_DELETE_CONFIRM_PREFIX}:"))
async def confirm_user_delete(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    user_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(callback.from_user.id)
        payload = await hub.accounts.delete_user_account(user_id, actor_admin_id=admin.id if admin else None)
    await render_admin(
        callback,
        f"Аккаунт {('@' + payload['username']) if payload.get('username') else payload['telegram_id']} удалён из локальной базы.",
    )


@router.callback_query(F.data.startswith(f"{USER_TRIAL_PREFIX}:"))
async def user_trial(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    user_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        try:
            await hub.billing.activate_trial(user_id)
            await callback.answer("Тест выдан.")
        except ConflictError as exc:
            await callback.answer(str(exc), show_alert=True)
    await show_user_card(callback, user_id, container)


@router.callback_query(F.data.startswith(f"{USER_ACTIVATE_PREFIX}:"))
async def user_activate(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    user_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        await hub.billing.reactivate_user(user_id)
    await callback.answer("Пользователь активирован.")
    await show_user_card(callback, user_id, container)


@router.callback_query(F.data.startswith(f"{USER_DEACTIVATE_PREFIX}:"))
async def user_deactivate(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    user_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        await hub.billing.deactivate_user(user_id)
    await callback.answer("Пользователь деактивирован.")
    await show_user_card(callback, user_id, container)


@router.callback_query(F.data.startswith(f"{USER_PLAN_PREFIX}:"))
async def user_plan(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    user_id, resolved_plan_code = parse_user_plan(callback.data)
    if resolved_plan_code is None:
        await callback.answer("Кнопка устарела, откройте карточку заново.", show_alert=True)
        return
    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(callback.from_user.id)
        try:
            await hub.billing.activate_paid_plan(
                user_id,
                resolved_plan_code,
                charge_user=False,
                admin_id=admin.id if admin else None,
            )
            await callback.answer("Тариф применён без списания.")
        except (ConflictError, NotFoundError, ServiceError) as exc:
            await callback.answer(str(exc), show_alert=True)
    await show_user_card(callback, user_id, container)


@router.callback_query(F.data.startswith(f"{USER_BALANCE_PREFIX}:"))
async def user_balance_prompt(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(user_id=callback.data.split(":")[-1])
    await render_admin(callback, "Введите сумму корректировки, например: -50 или 500")


@router.message(BalanceStates.waiting_for_amount)
async def user_balance_apply(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    if await route_admin_menu_action(message, state, container):
        return

    data = await state.get_data()
    user_id = data["user_id"]
    try:
        amount = Decimal((message.text or "").replace(",", "."))
    except (InvalidOperation, AttributeError):
        await render_admin(message, "Не удалось распознать сумму. Попробуйте ещё раз.")
        return

    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(message.from_user.id)
        await hub.accounts.adjust_balance(
            user_id=user_id,
            amount_rub=amount,
            transaction_type=BalanceTransactionType.MANUAL_ADJUSTMENT,
            description="Ручная корректировка через admin bot",
            admin_id=admin.id if admin else None,
        )
    await state.clear()
    await show_user_card(message, user_id, container)


@router.message(F.text == "Серверы")
async def servers_screen(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        items = await hub.catalog.list_servers()
    await render_admin(
        message,
        "Серверы\n\n"
        "Синхронизация подтягивает ноды из Remnawave в локальную базу.\n"
        "Локальное включение регулирует доступ пользователей, а тип сервера определяет роль в тарифах.",
        reply_markup=server_actions("sync", True).as_markup(),
    )
    for server in items[:12]:
        sent = await message.answer(format_server_card(server), reply_markup=server_actions(server.id, server.is_available).as_markup())
        remember_admin_card(sent)


@router.callback_query(F.data == "admin:server_toggle:sync:0")
async def sync_servers(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    async with container.hub() as hub:
        servers = await hub.catalog.sync_servers()
    await render_admin(callback, f"Синхронизация завершена. Серверов: {len(servers)}")


@router.callback_query(F.data.startswith("admin:server_toggle:"))
async def toggle_server(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    _, _, server_id, flag = callback.data.split(":", 3)
    if server_id == "sync":
        await sync_servers(callback, container)
        return
    async with container.hub() as hub:
        server = await hub.catalog.set_server_availability(server_id, flag == "1")
    if server is None:
        await render_admin(callback, "Локальный статус сервера обновлён.")
        return
    await render_admin(
        callback,
        format_server_card(server),
        reply_markup=server_actions(server.id, server.is_available).as_markup(),
    )


@router.callback_query(F.data.startswith("admin:server_type:"))
async def change_server_type(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    _, _, server_id, raw_type = callback.data.split(":", 3)
    async with container.hub() as hub:
        server = await hub.catalog.set_server_type(server_id, ServerType(raw_type))
    await render_admin(
        callback,
        format_server_card(server),
        reply_markup=server_actions(
            getattr(server, "id", server_id),
            getattr(server, "is_available", True),
        ).as_markup(),
    )


@router.message(F.text == "Аналитика")
async def statistics(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        overview = await hub.dashboard.overview()
        panel = await hub.dashboard.panel_status()
    lines = [
        "Аналитика",
        "",
        *format_panel_status(panel),
        "",
        f"Активные: {overview['active_users']}",
        f"Без продления: {overview['renewal_disabled_users']}",
        f"Заблокированные: {overview['blocked_users']}",
        f"Тестовые: {overview['trial_users']}",
        f"Платежей за 30 дней: {overview['payments_count']}",
        f"Выручка за 30 дней: {overview['payments_total_rub']:.2f} ₽",
        f"Трафик: {overview['total_traffic_bytes'] / 1024**3:.2f} ГБ",
        f"Whitelist-трафик: {overview['whitelist_traffic_bytes'] / 1024**3:.2f} ГБ",
    ]
    await render_admin(message, "\n".join(lines))


@router.message(F.text == "Онлайн")
async def online(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        await hub.online.refresh_online_cache(detailed=True)
        records = await hub.online.list_online()
        if not records:
            records = await hub.online.list_online(only_online=False)
        enriched = [(item, hub.online.session_summary(item)) for item in records[:12]]
    if not enriched:
        await render_admin(message, "Данных по online-сессиям пока нет.")
        return

    blocks = ["Онлайн клиенты", ""]
    for item, summary in enriched:
        blocks.extend(
            [
                f"{user_label(item.user)}",
                f"Статус: {summary['current_status']}",
                f"Сервер: {item.server.name if item.server else 'n/a'}",
                f"IP: {summary.get('last_ip') or 'n/a'}",
                f"Устройство: {summary.get('current_device')}",
                f"Уникальных устройств: {summary.get('unique_device_count', 0)}",
                f"Уникальных IP: {summary.get('unique_ip_count', 0)}",
                f"Последняя активность: {item.last_activity_at.strftime('%d.%m %H:%M') if item.last_activity_at else '—'}",
                "",
            ]
        )
    await render_admin(message, "\n".join(blocks).strip())


@router.message(F.text == "Топы")
async def top_users_menu(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        rows = await hub.dashboard.top_users("traffic")
    await render_admin(message, format_top_users("traffic", rows), reply_markup=top_users_actions("traffic").as_markup())


@router.callback_query(F.data.startswith("admin:tops:"))
async def top_users(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    metric = callback.data.split(":")[-1]
    if metric not in TOP_METRIC_LABELS:
        await callback.answer("Неизвестный рейтинг.", show_alert=True)
        return
    async with container.hub() as hub:
        rows = await hub.dashboard.top_users(metric)
    await render_admin(callback, format_top_users(metric, rows), reply_markup=top_users_actions(metric).as_markup())


@router.message(F.text == "Запросы поддержки")
async def support_requests_screen(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        items = await hub.support.list_requests(limit=20)
    if not items:
        await render_admin(message, "Запросов поддержки пока нет.")
        return
    open_count = len([item for item in items if item.status == SupportRequestStatus.NEW])
    await render_admin(message, f"Запросы поддержки\n\nВсего показано: {len(items)}\nОткрытых: {open_count}")
    for item in items[:10]:
        sent = await message.answer(
            format_support_request(item),
            reply_markup=support_request_actions(item.id, item.status == SupportRequestStatus.RESOLVED).as_markup(),
        )
        remember_admin_card(sent)


@router.callback_query(F.data == "admin:support:list")
async def refresh_support_requests(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    async with container.hub() as hub:
        items = await hub.support.list_requests(limit=20)
    if not items:
        await render_admin(callback, "Запросов поддержки пока нет.")
        return
    await render_admin(callback, format_support_request(items[0]), reply_markup=support_request_actions(items[0].id, items[0].status == SupportRequestStatus.RESOLVED).as_markup())


@router.callback_query(F.data.startswith("admin:support:resolve:"))
async def resolve_support_request(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    request_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(callback.from_user.id)
        item = await hub.support.resolve_request(
            request_id,
            admin_id=admin.id if admin else None,
            resolution_comment="Закрыто через admin bot",
        )
    await render_admin(callback, format_support_request(item), reply_markup=support_request_actions(item.id, True).as_markup())


@router.message(F.text == "Промокоды")
async def promo_codes_screen(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        items = await hub.promos.list_codes(limit=20)
    await render_admin(message, format_promo_list(items), reply_markup=build_promo_list_markup(items))


@router.message(F.text == "Создать промокод")
async def promo_create_reply(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    await state.set_state(PromoStates.waiting_for_payload)
    await render_admin(message, promo_create_help_text(), reply_markup=promo_list_actions().as_markup())


@router.callback_query(F.data == "admin:promo:list")
async def promo_codes_refresh(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    async with container.hub() as hub:
        items = await hub.promos.list_codes(limit=20)
    await render_admin(callback, format_promo_list(items), reply_markup=build_promo_list_markup(items))


@router.callback_query(F.data == "admin:promo:new")
async def promo_create_prompt(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await state.set_state(PromoStates.waiting_for_payload)
    await render_admin(callback, promo_create_help_text(), reply_markup=promo_list_actions().as_markup())


@router.callback_query(F.data.startswith(f"{PROMO_TOGGLE_PREFIX}:"))
async def promo_toggle(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    _, _, promo_id, flag = callback.data.split(":", 3)
    async with container.hub() as hub:
        promo = await hub.session.get(PromoCode, promo_id)
        if promo is None:
            await callback.answer("Промокод не найден.", show_alert=True)
            return
        promo.is_active = flag == "1"
        await hub.session.flush()
        items = await hub.promos.list_codes(limit=20)
    await render_admin(callback, format_promo_list(items), reply_markup=build_promo_list_markup(items))


@router.message(PromoStates.waiting_for_payload)
async def promo_create_submit(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    if await route_admin_menu_action(message, state, container):
        return
    try:
        payload = parse_promo_payload(message.text or "")
    except ConflictError as exc:
        await render_admin(message, f"{exc}\n\n{promo_create_help_text()}")
        return

    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(message.from_user.id)
        try:
            await hub.promos.create_code(admin_id=admin.id if admin else None, **payload)
        except ConflictError as exc:
            await render_admin(message, f"{exc}\n\n{promo_create_help_text()}")
            return
        items = await hub.promos.list_codes(limit=20)
    await state.clear()
    await render_admin(message, format_promo_list(items), reply_markup=build_promo_list_markup(items))


@router.message(F.text == "Рассылка")
async def broadcast_prompt(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    await state.set_state(BroadcastStates.waiting_for_text)
    await state.update_data(broadcast_text=None, broadcast_file_id=None, broadcast_use_default=False)
    await render_admin(
        message,
        "Рассылка\n\nОтправьте текст сообщения. Следующим шагом можно будет приложить свою картинку или использовать логотип сервиса.",
    )


@router.message(BroadcastStates.waiting_for_text)
async def broadcast_text_submit(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    if await route_admin_menu_action(message, state, container):
        return
    text = (message.text or "").strip()
    if not text:
        await render_admin(message, "Текст рассылки не должен быть пустым.")
        return
    await state.update_data(broadcast_text=text)
    await state.set_state(BroadcastStates.waiting_for_media)
    await render_admin(
        message,
        "Текст рассылки сохранён.\n\nТеперь отправьте фото одним сообщением или нажмите кнопку ниже, чтобы использовать логотип сервиса.",
        reply_markup=broadcast_media_actions().as_markup(),
    )


@router.callback_query(F.data == "admin:broadcast:default")
async def broadcast_use_default(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await state.update_data(broadcast_use_default=True, broadcast_file_id=None)
    await show_broadcast_preview(callback, state, use_default_logo=True)


@router.callback_query(F.data == "admin:broadcast:cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await state.clear()
    await render_admin(callback, "Создание рассылки отменено.")


@router.message(BroadcastStates.waiting_for_media, F.photo)
async def broadcast_media_submit(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    await state.update_data(
        broadcast_file_id=message.photo[-1].file_id,
        broadcast_use_default=False,
    )
    await show_broadcast_preview(message, state, use_default_logo=False)


@router.message(BroadcastStates.waiting_for_media)
async def broadcast_media_fallback(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    if await route_admin_menu_action(message, state, container):
        return
    await render_admin(
        message,
        "Ожидается фото или кнопка «Использовать логотип». Если картинка не нужна, нажмите кнопку ниже.",
        reply_markup=broadcast_media_actions().as_markup(),
    )


@router.callback_query(F.data == "admin:broadcast:confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    if not container.settings.client_bot_token:
        await callback.answer("CLIENT_BOT_TOKEN не задан.", show_alert=True)
        return
    data = await state.get_data()
    text = (data.get("broadcast_text") or "").strip()
    file_id = data.get("broadcast_file_id")
    use_default_logo = bool(data.get("broadcast_use_default"))
    if not text:
        await callback.answer("Текст рассылки потерян, начните заново.", show_alert=True)
        await state.clear()
        return

    media_bytes = await load_broadcast_media_bytes(callback, file_id)
    media_filename = f"broadcast-{datetime.now(UTC):%Y%m%d%H%M%S}.jpg"
    failure_counts: Counter[str] = Counter()
    failure_examples: list[str] = []

    async with container.hub() as hub:
        users = await hub.accounts.list_user_targets()
        sent_count = 0
        failed_count = 0
        logo = broadcast_logo_path() if use_default_logo and not file_id else None
        client_bot = Bot(token=container.settings.client_bot_token)
        try:
            for user in users:
                try:
                    if media_bytes is not None:
                        await client_bot.send_photo(
                            user.telegram_id,
                            photo=BufferedInputFile(media_bytes, filename=media_filename),
                            caption=text,
                        )
                    elif logo is not None:
                        await client_bot.send_photo(user.telegram_id, photo=FSInputFile(str(logo)), caption=text)
                    else:
                        await client_bot.send_message(user.telegram_id, text)
                    sent_count += 1
                except Exception as exc:  # noqa: BLE001
                    failed_count += 1
                    reason = broadcast_failure_reason(exc)
                    failure_counts[reason] += 1
                    if len(failure_examples) < 5:
                        failure_examples.append(f"{user_label(user)} — {reason}")
                    logger.warning("Broadcast send failed for telegram_id=%s: %s", user.telegram_id, exc)
        finally:
            await client_bot.session.close()
        await hub.accounts.log_event(
            level=SystemEventLevel.INFO,
            event_type="broadcast_sent",
            message="Администратор отправил рассылку пользователям.",
            payload={
                "sent": sent_count,
                "failed": failed_count,
                "failure_reasons": dict(failure_counts),
            },
        )
    await state.clear()
    lines = [
        "Рассылка завершена.",
        "",
        f"Отправлено: {sent_count}",
        f"Ошибок: {failed_count}",
    ]
    if failure_counts:
        lines.extend(["", "Почему часть сообщений не доставлена:"])
        for reason, count in failure_counts.most_common():
            lines.append(f"• {reason}: {count}")
    if failure_examples:
        lines.extend(["", "Примеры:"])
        lines.extend(failure_examples)
    await render_admin(callback, "\n".join(lines))


@router.message(F.text == "Логи")
async def system_logs_screen(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        events = await hub.dashboard.list_events(limit=12)
        status = await hub.dashboard.panel_status()
    await render_admin(message, format_system_log_screen(status, events), reply_markup=system_logs_actions().as_markup())


@router.callback_query(F.data == "admin:logs:refresh")
async def refresh_system_logs(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    async with container.hub() as hub:
        events = await hub.dashboard.list_events(limit=12)
        status = await hub.dashboard.panel_status()
    await render_admin(callback, format_system_log_screen(status, events), reply_markup=system_logs_actions().as_markup())


@router.message(UserLookupStates.waiting_for_query)
async def user_lookup_state_handler(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    if await route_admin_menu_action(message, state, container):
        return
    query = (message.text or "").strip()
    if not query:
        await render_admin(message, USER_LOOKUP_PROMPT)
        return
    await process_user_lookup(message, container)


@router.message()
async def admin_message_fallback(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    if await route_admin_menu_action(message, state, container):
        return

    current_state = await state.get_state()
    if current_state in {
        BalanceStates.waiting_for_amount.state,
        PromoStates.waiting_for_payload.state,
        BroadcastStates.waiting_for_text.state,
        BroadcastStates.waiting_for_media.state,
    }:
        return

    if admin_mode(message.from_user.id) == "user_search" or is_user_lookup_query(message.text):
        set_admin_mode(message.from_user.id, "user_search")
        await process_user_lookup(message, container)
