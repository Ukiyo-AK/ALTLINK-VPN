from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

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
from altlink.infrastructure.db.models import PromoCode, TrafficSnapshot
from altlink.presentation.bots.admin_keyboards import (
    DATABASE_BACKUP_CANCEL_IMPORT,
    DATABASE_BACKUP_CONFIRM_IMPORT,
    DATABASE_BACKUP_EXPORT,
    DATABASE_BACKUP_IMPORT,
    DATABASE_BACKUP_OPEN,
    MAINTENANCE_ADD_EXCEPTION,
    MAINTENANCE_CANCEL,
    MAINTENANCE_OPEN,
    MAINTENANCE_PICK_ADD_PREFIX,
    MAINTENANCE_REMOVE_PREFIX,
    MAINTENANCE_TOGGLE,
    PROMO_TOGGLE_PREFIX,
    PAYMENT_APPROVE_PREFIX,
    PAYMENT_PAGE_PREFIX,
    PAYMENT_REFRESH_PREFIX,
    PAYMENT_REJECT_PREFIX,
    SERVER_DELETE_CONFIRM_PREFIX,
    SERVER_DELETE_PREFIX,
    SERVER_OPEN_PREFIX,
    USER_ACTIVATE_PREFIX,
    USER_BALANCE_PREFIX,
    USER_MESSAGE_CANCEL_PREFIX,
    USER_MESSAGE_PREFIX,
    USER_DEACTIVATE_PREFIX,
    USER_DELETE_CONFIRM_PREFIX,
    USER_DELETE_PREFIX,
    USER_OPEN_PREFIX,
    USER_PLAN_PREFIX,
    USER_SUBSCRIPTIONS_PREFIX,
    USER_TRIAL_PREFIX,
    admin_menu,
    broadcast_media_actions,
    database_backup_actions,
    database_import_confirmation_actions,
    database_import_prompt_actions,
    broadcast_preview_actions,
    maintenance_actions,
    maintenance_prompt_actions,
    maintenance_user_pick_actions,
    payment_browser_actions,
    promo_list_actions,
    server_actions,
    server_delete_confirmation_actions,
    support_request_actions,
    system_logs_actions,
    top_users_actions,
    user_actions,
    user_delete_confirmation_actions,
    user_lookup_actions,
    user_message_prompt_actions,
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
    "База данных",
    "Техработы",
    "Помощь",
}
TOP_METRIC_LABELS = {
    "traffic": "общему трафику",
    "whitelist": "трафику по белым спискам",
    "balance": "балансу",
    "topups": "сумме пополнений",
}


async def sync_dashboard_traffic_if_possible(container: AppContainer) -> None:
    try:
        async with container.hub() as sync_hub:
            billing = getattr(sync_hub, "billing", None)
            if billing is None:
                return
            await asyncio.wait_for(billing.snapshot_traffic(), timeout=8)
    except TimeoutError:
        logger.warning("Timed out while syncing traffic snapshots before admin dashboard render.")
    except Exception:
        logger.warning("Failed to sync traffic snapshots before admin dashboard render.", exc_info=True)


async def sync_server_catalog_if_possible(hub) -> None:
    sync_method = getattr(getattr(hub, "catalog", None), "sync_servers", None)
    if sync_method is None:
        return
    try:
        await sync_method()
    except Exception:
        logger.warning("Failed to sync server catalog before admin server render.", exc_info=True)


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
ADMIN_PENDING_DATABASE_IMPORTS: dict[int, bytes] = {}


class BalanceStates(StatesGroup):
    waiting_for_amount = State()


class UserLookupStates(StatesGroup):
    waiting_for_query = State()


class PromoStates(StatesGroup):
    waiting_for_payload = State()


class BroadcastStates(StatesGroup):
    waiting_for_text = State()
    waiting_for_media = State()


class DirectMessageStates(StatesGroup):
    waiting_for_text = State()


class DatabaseImportStates(StatesGroup):
    waiting_for_document = State()
    waiting_for_confirmation = State()


class MaintenanceStates(StatesGroup):
    waiting_for_add_exception_query = State()


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


def clear_pending_database_import(telegram_id: int) -> None:
    ADMIN_PENDING_DATABASE_IMPORTS.pop(telegram_id, None)


def set_pending_database_import(telegram_id: int, payload: bytes) -> None:
    ADMIN_PENDING_DATABASE_IMPORTS[telegram_id] = payload


def get_pending_database_import(telegram_id: int) -> bytes | None:
    return ADMIN_PENDING_DATABASE_IMPORTS.get(telegram_id)


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


def format_database_backup_screen() -> str:
    return (
        "База данных\n\n"
        "Экспорт отправит JSON backup текущей локальной базы.\n"
        "Импорт полностью заменит текущую локальную базу содержимым backup-файла.\n\n"
        "Remnawave и внешние сервисы отдельно не импортируются."
    )


def format_database_backup_summary(summary: dict, *, title: str) -> str:
    counts = summary.get("table_counts") or {}
    interesting_tables = [
        ("admin_users", "Админы"),
        ("users", "Пользователи"),
        ("subscriptions", "Подписки"),
        ("servers", "Серверы"),
        ("topup_requests", "Платежи"),
        ("system_events", "События"),
    ]
    lines = [
        title,
        "",
        f"Формат: {summary.get('format', 'unknown')}",
        f"Экспортирован: {summary.get('exported_at', 'n/a')}",
        f"Источник БД: {summary.get('database_dialect', 'unknown')}",
        f"Всего записей: {summary.get('total_rows', 0)}",
    ]
    for table_name, label in interesting_tables:
        if table_name in counts:
            lines.append(f"{label}: {counts[table_name]}")
    return "\n".join(lines)


def format_database_import_confirmation(summary: dict) -> str:
    return (
        format_database_backup_summary(summary, title="Резервная копия загружена")
        + "\n\nИмпорт заменит текущую локальную базу целиком. Продолжить?"
    )


def format_maintenance_screen(manual_state: dict, *, automatic_active: bool) -> str:
    exceptions = list((manual_state or {}).get("exceptions") or [])
    lines = [
        "Техработы клиентского бота",
        "",
        f"Ручной режим: {'включён' if manual_state.get('enabled') else 'выключен'}",
        f"Авто-режим из-за недоступности панели: {'активен' if automatic_active else 'не активен'}",
    ]
    updated_at = manual_state.get("updated_at")
    if updated_at:
        lines.append(f"Последнее изменение: {updated_at}")
    lines.extend(
        [
            "",
            "При включённом ручном режиме обычные пользователи увидят сообщение о технических работах.",
            "Исключения работают только для ручного режима и не обходят недоступность Remnawave.",
            "",
            "Исключения:",
        ]
    )
    if exceptions:
        for item in exceptions:
            label = str(item.get("label") or item.get("telegram_id") or item.get("user_id"))
            lines.append(f"• {label}")
    else:
        lines.append("Исключений пока нет.")
    return "\n".join(lines)


def maintenance_exception_prompt_text() -> str:
    return (
        "Добавить исключение\n\n"
        "Отправьте Telegram ID, @username, локальный UUID или Remnawave UUID пользователя.\n"
        "Этот пользователь сможет пользоваться клиентским ботом даже при включённых ручных техработах."
    )


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


def payment_provider_code(item) -> str:
    provider = str(getattr(item, "provider_code", "") or "").strip().lower()
    return provider or "manual"


def payment_provider_label(item) -> str:
    provider = payment_provider_code(item)
    return {
        "manual": "через поддержку",
        "yookassa": "Юкасса СБП",
        "stub": "тестовая касса",
    }.get(provider, provider)


def payment_requires_manual_resolution(item) -> bool:
    return str(getattr(item, "status", "")) == "new" and payment_provider_code(item) == "manual"


def payment_status_label(item) -> str:
    status = str(getattr(item, "status", ""))
    provider = payment_provider_code(item)
    if status == "new":
        return "незавершён" if provider == "yookassa" else "ожидает решения"
    if status == "approved":
        return "оплачен автоматически" if provider == "yookassa" else "подтверждён"
    if status == "rejected":
        return "отменён кассой" if provider == "yookassa" else "отклонён"
    if status == "canceled":
        return "отменён пользователем"
    return status


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


def format_payment_browser(items, active_index: int) -> str:
    if not items:
        return "Платежи\n\nПлатежей пока нет."

    current = items[active_index]
    counts = Counter(str(item.status) for item in items)
    lines = [
        f"Платежи {active_index + 1}/{len(items)}",
        "",
        f"Ожидают решения: {counts.get('new', 0)}",
        f"Подтверждены: {counts.get('approved', 0)}",
        f"Отклонены: {counts.get('rejected', 0)}",
    ]
    if counts.get("canceled", 0):
        lines.append(f"Отменены: {counts.get('canceled', 0)}")
    lines.extend(["", format_payment_request(current)])
    return "\n".join(lines)


def format_payment_request(item) -> str:
    lines = [
        "Р—Р°СЏРІРєР° РЅР° РїРѕРїРѕР»РЅРµРЅРёРµ",
        "",
        f"РќРѕРјРµСЂ: {item.id}",
        f"РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ: {user_label(item.user)}",
        f"Telegram ID: {item.user.telegram_id if item.user else 'n/a'}",
        f"РЎСѓРјРјР°: {Decimal(item.amount_rub):.2f} в‚Ѕ",
        f"РЎРїРѕСЃРѕР±: {payment_provider_label(item)}",
        f"РЎС‚Р°С‚СѓСЃ: {payment_status_label(item)}",
        f"РЎРѕР·РґР°РЅ: {item.created_at:%d.%m.%Y %H:%M}",
    ]
    if item.user_comment:
        lines.extend(["", f"РљРѕРјРјРµРЅС‚Р°СЂРёР№ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ: {item.user_comment}"])
    if item.admin_comment:
        if payment_provider_code(item) == "yookassa" and str(item.admin_comment).startswith("yookassa:"):
            lines.extend(["", f"РўРµС…РЅРёС‡РµСЃРєР°СЏ РјРµС‚РєР°: {item.admin_comment}"])
        else:
            lines.extend(["", f"РљРѕРјРјРµРЅС‚Р°СЂРёР№ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°: {item.admin_comment}"])
    if payment_provider_code(item) == "yookassa" and str(item.status) == "new":
        lines.extend(["", "РџР»Р°С‚РµР¶ РїСЂРѕРІРµСЂСЏРµС‚СЃСЏ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё. Р СѓС‡РЅРѕРµ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёРµ РЅРµ С‚СЂРµР±СѓРµС‚СЃСЏ."])
    return "\n".join(lines)


def format_payment_browser(items, active_index: int) -> str:
    if not items:
        return "РџР»Р°С‚РµР¶Рё\n\nРџР»Р°С‚РµР¶РµР№ РїРѕРєР° РЅРµС‚."

    current = items[active_index]
    counts = Counter(str(item.status) for item in items)
    manual_pending = sum(1 for item in items if payment_requires_manual_resolution(item))
    yookassa_unfinished = sum(
        1 for item in items if str(getattr(item, "status", "")) == "new" and payment_provider_code(item) == "yookassa"
    )
    lines = [
        f"РџР»Р°С‚РµР¶Рё {active_index + 1}/{len(items)}",
        "",
        f"РћР¶РёРґР°СЋС‚ СЂРµС€РµРЅРёСЏ: {manual_pending}",
        f"РќРµР·Р°РІРµСЂС€С‘РЅРЅС‹Рµ Р®РєР°СЃСЃР° РЎР‘Рџ: {yookassa_unfinished}",
        f"РџРѕРґС‚РІРµСЂР¶РґРµРЅС‹: {counts.get('approved', 0)}",
        f"РћС‚РєР»РѕРЅРµРЅС‹: {counts.get('rejected', 0)}",
    ]
    if counts.get("canceled", 0):
        lines.append(f"РћС‚РјРµРЅРµРЅС‹: {counts.get('canceled', 0)}")
    lines.extend(["", format_payment_request(current)])
    return "\n".join(lines)


def format_panel_status(status: dict) -> list[str]:
    remnawave_label = "OK" if status.get("remnawave_ok") else "ошибка"
    return [
        "Состояние панели",
        f"Remnawave: {remnawave_label}",
        f"База данных: {status.get('database_dialect', 'unknown')}",
        f"Размер БД: {status.get('db_size_gb', 0)} ГБ",
    ]


def payment_provider_label(item) -> str:
    provider = payment_provider_code(item)
    return {
        "manual": "\u0447\u0435\u0440\u0435\u0437 \u043f\u043e\u0434\u0434\u0435\u0440\u0436\u043a\u0443",
        "yookassa": "\u042e\u043a\u0430\u0441\u0441\u0430 \u0421\u0411\u041f",
        "stub": "\u0442\u0435\u0441\u0442\u043e\u0432\u0430\u044f \u043a\u0430\u0441\u0441\u0430",
    }.get(provider, provider)


def payment_status_label(item) -> str:
    status = str(getattr(item, "status", ""))
    provider = payment_provider_code(item)
    if status == "new":
        return "\u043d\u0435\u0437\u0430\u0432\u0435\u0440\u0448\u0451\u043d" if provider == "yookassa" else "\u043e\u0436\u0438\u0434\u0430\u0435\u0442 \u0440\u0435\u0448\u0435\u043d\u0438\u044f"
    if status == "approved":
        return "\u043e\u043f\u043b\u0430\u0447\u0435\u043d \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438" if provider == "yookassa" else "\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d"
    if status == "rejected":
        return "\u043e\u0442\u043c\u0435\u043d\u0451\u043d \u043a\u0430\u0441\u0441\u043e\u0439" if provider == "yookassa" else "\u043e\u0442\u043a\u043b\u043e\u043d\u0451\u043d"
    if status == "canceled":
        return "\u043e\u0442\u043c\u0435\u043d\u0451\u043d \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0435\u043c"
    return status


def format_payment_request(item) -> str:
    lines = [
        "\u0417\u0430\u044f\u0432\u043a\u0430 \u043d\u0430 \u043f\u043e\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u0435",
        "",
        f"\u041d\u043e\u043c\u0435\u0440: {item.id}",
        f"\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c: {user_label(item.user)}",
        f"Telegram ID: {item.user.telegram_id if item.user else 'n/a'}",
        f"\u0421\u0443\u043c\u043c\u0430: {Decimal(item.amount_rub):.2f} \u20bd",
        f"\u0421\u043f\u043e\u0441\u043e\u0431: {payment_provider_label(item)}",
        f"\u0421\u0442\u0430\u0442\u0443\u0441: {payment_status_label(item)}",
        f"\u0421\u043e\u0437\u0434\u0430\u043d: {item.created_at:%d.%m.%Y %H:%M}",
    ]
    if item.user_comment:
        lines.extend(["", f"\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f: {item.user_comment}"])
    if item.admin_comment:
        if payment_provider_code(item) == "yookassa" and str(item.admin_comment).startswith("yookassa:"):
            lines.extend(["", f"\u0422\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0430\u044f \u043c\u0435\u0442\u043a\u0430: {item.admin_comment}"])
        else:
            lines.extend(["", f"\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0430: {item.admin_comment}"])
    if payment_provider_code(item) == "yookassa" and str(item.status) == "new":
        lines.extend(["", "\u041f\u043b\u0430\u0442\u0451\u0436 \u043f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u0442\u0441\u044f \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438. \u0420\u0443\u0447\u043d\u043e\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435 \u043d\u0435 \u0442\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f."])
    return "\n".join(lines)


def format_payment_browser(items, active_index: int) -> str:
    if not items:
        return "\u041f\u043b\u0430\u0442\u0435\u0436\u0438\n\n\u041f\u043b\u0430\u0442\u0435\u0436\u0435\u0439 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442."

    current = items[active_index]
    counts = Counter(str(item.status) for item in items)
    manual_pending = sum(1 for item in items if payment_requires_manual_resolution(item))
    yookassa_unfinished = sum(
        1 for item in items if str(getattr(item, "status", "")) == "new" and payment_provider_code(item) == "yookassa"
    )
    lines = [
        f"\u041f\u043b\u0430\u0442\u0435\u0436\u0438 {active_index + 1}/{len(items)}",
        "",
        f"\u041e\u0436\u0438\u0434\u0430\u044e\u0442 \u0440\u0435\u0448\u0435\u043d\u0438\u044f: {manual_pending}",
        f"\u041d\u0435\u0437\u0430\u0432\u0435\u0440\u0448\u0451\u043d\u043d\u044b\u0435 \u042e\u043a\u0430\u0441\u0441\u0430 \u0421\u0411\u041f: {yookassa_unfinished}",
        f"\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u044b: {counts.get('approved', 0)}",
        f"\u041e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u044b: {counts.get('rejected', 0)}",
    ]
    if counts.get("canceled", 0):
        lines.append(f"\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u044b: {counts.get('canceled', 0)}")
    lines.extend(["", format_payment_request(current)])
    return "\n".join(lines)


def format_server_card(server) -> str:
    type_label = {
        ServerType.TEN_GBIT: "Start",
        ServerType.WHITELIST: "Whitelist",
        ServerType.REGULAR: "Обычный",
    }.get(getattr(server, "server_type", ServerType.REGULAR), "Обычный")
    capacity = getattr(server, "max_clients", 0) or 0
    access_count = getattr(server, "current_clients", 0) or 0
    online_count = getattr(server, "users_online", 0) or 0
    if capacity > 0:
        load_label = f"{getattr(server, 'load_percent', 0)}% ({online_count}/{capacity} online)"
        capacity_label = str(capacity)
    else:
        load_label = "не рассчитывается, пока не задана ёмкость"
        capacity_label = "не задана"
    return (
        f"{getattr(server, 'name', 'Сервер')}\n"
        f"Адрес: {getattr(server, 'address', '—')}\n"
        f"Тип: {type_label}\n"
        f"Локально: {'включён' if getattr(server, 'is_available', False) else 'выключен'}\n"
        f"Статус: {'online' if getattr(server, 'is_connected', False) else 'offline'}\n"
        f"Выдан доступ: {access_count}\n"
        f"Онлайн сейчас: {online_count}\n"
        f"Ёмкость: {capacity_label}\n"
        f"Нагрузка: {load_label}"
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
    elif text == "База данных":
        await database_backup_screen(message, state, container)
    elif text == "Техработы":
        await maintenance_screen(message, state, container)
    else:
        await admin_help(message, container)
    return True


async def database_backup_screen(target: Message | CallbackQuery, state: FSMContext, container: AppContainer) -> None:
    if hasattr(target, "from_user") and getattr(target.from_user, "id", None) is not None:
        clear_pending_database_import(target.from_user.id)
    await state.clear()
    await render_admin(target, format_database_backup_screen(), reply_markup=database_backup_actions().as_markup())


async def render_manual_maintenance_screen(
    target: Message | CallbackQuery,
    *,
    container: AppContainer,
    manual_state: dict,
) -> None:
    automatic_active = False
    async with container.hub() as hub:
        dashboard = getattr(hub, "dashboard", None)
        if getattr(container.settings, "remnawave_base_url", "") and dashboard is not None:
            status = await dashboard.panel_status()
            automatic_active = not bool(status.get("remnawave_ok"))
    await render_admin(
        target,
        format_maintenance_screen(manual_state, automatic_active=automatic_active),
        reply_markup=maintenance_actions(manual_state).as_markup(),
    )


async def maintenance_screen(
    target: Message | CallbackQuery,
    state: FSMContext | None,
    container: AppContainer,
) -> None:
    if state is not None:
        await state.clear()
    async with container.hub() as hub:
        manual_state = await hub.monitoring.get_manual_client_maintenance_state()
    await render_manual_maintenance_screen(target, container=container, manual_state=manual_state)


async def show_user_card(target: Message | CallbackQuery, user_id: str, container: AppContainer):
    async with container.hub() as hub:
        card = await hub.accounts.user_card(user_id)
        user = card["user"]
        subscription = card["subscription"]
        latest_snapshot = None
        if subscription is not None and getattr(user, "remnawave_user_uuid", None) and getattr(hub, "billing", None):
            try:
                refreshed_subscription = await hub.billing.refresh_subscription_traffic(user.id)
            except Exception:
                logger.warning("Failed to refresh subscription traffic for user_id=%s before rendering admin card.", user.id)
            else:
                if refreshed_subscription is not None:
                    subscription = refreshed_subscription
        if getattr(hub, "session", None) is not None:
            latest_snapshot = await hub.session.scalar(
                select(TrafficSnapshot)
                .where(TrafficSnapshot.user_id == user.id)
                .order_by(TrafficSnapshot.snapshot_date.desc(), TrafficSnapshot.created_at.desc())
                .limit(1)
            )
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
        lines.append(f"?????? ?? ??????? ????: {subscription.traffic_used_bytes / 1024**3:.2f} ??")
        if latest_snapshot is not None:
            lines.append(f"????? ?????? ? ??????: {latest_snapshot.lifetime_used_bytes / 1024**3:.2f} ??")
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


async def show_direct_message_prompt(target: Message | CallbackQuery, user_id: str, container: AppContainer):
    async with container.hub() as hub:
        user = await hub.accounts.get_user(user_id)
    await render_admin(
        target,
        "Личное сообщение клиенту\n\n"
        f"Пользователь: {user_label(user)}\n"
        f"Telegram ID: {user.telegram_id}\n\n"
        "Отправьте текст, файл, медиа, gif, голосовое, кружок или стикер следующим сообщением.\n"
        "Если нужен текст вместе с вложением, добавьте подпись. Сообщение уйдёт от имени клиентского бота.",
        reply_markup=user_message_prompt_actions(user_id).as_markup(),
    )


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


async def render_payment_browser(
    target: Message | CallbackQuery,
    container: AppContainer,
    *,
    index: int = 0,
    request_id: str | None = None,
) -> None:
    async with container.hub() as hub:
        await hub.topups.sync_pending_yookassa_checkouts()
        items = await hub.topups.list_requests()

    if not items:
        await render_admin(target, "Платежи\n\nПлатежей пока нет.")
        return

    active_index = max(0, min(index, len(items) - 1))
    if request_id is not None:
        active_index = next((idx for idx, item in enumerate(items) if item.id == request_id), active_index)
    current = items[active_index]
    await render_admin(
        target,
        format_payment_browser(items, active_index),
        reply_markup=payment_browser_actions(
            request_id=current.id,
            status=str(current.status),
            index=active_index,
            total=len(items),
            allow_manual_resolution=payment_requires_manual_resolution(current),
        ).as_markup(),
    )


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


def outbound_message_text(message: Message) -> str:
    return ((message.text or message.caption or "") if message is not None else "").strip()


def outbound_attachment_label(kind: str | None) -> str:
    return {
        "photo": "фото",
        "video": "видео",
        "document": "файл",
        "animation": "gif / анимация",
        "audio": "аудио",
        "voice": "голосовое",
        "video_note": "кружок",
        "sticker": "стикер",
    }.get(kind or "", "вложение")


def outbound_attachment_supports_caption(kind: str | None) -> bool:
    return kind in {"photo", "video", "document", "animation", "audio", "voice"}


def outbound_attachment_filename(kind: str, file_name: str | None = None, *, is_animated: bool = False, is_video: bool = False) -> str:
    if file_name:
        return file_name
    defaults = {
        "photo": "attachment.jpg",
        "video": "attachment.mp4",
        "document": "attachment.bin",
        "animation": "attachment.mp4",
        "audio": "attachment.mp3",
        "voice": "attachment.ogg",
        "video_note": "attachment.mp4",
    }
    if kind == "sticker":
        if is_video:
            return "attachment.webm"
        if is_animated:
            return "attachment.tgs"
        return "attachment.webp"
    return defaults.get(kind, "attachment.bin")


def extract_outbound_attachment(message: Message) -> dict | None:
    photo = getattr(message, "photo", None)
    if photo:
        item = photo[-1]
        return {"kind": "photo", "file_id": item.file_id, "filename": outbound_attachment_filename("photo")}

    animation = getattr(message, "animation", None)
    if animation:
        return {
            "kind": "animation",
            "file_id": animation.file_id,
            "filename": outbound_attachment_filename("animation", getattr(animation, "file_name", None)),
        }

    document = getattr(message, "document", None)
    if document:
        return {
            "kind": "document",
            "file_id": document.file_id,
            "filename": outbound_attachment_filename("document", getattr(document, "file_name", None)),
        }

    video = getattr(message, "video", None)
    if video:
        return {
            "kind": "video",
            "file_id": video.file_id,
            "filename": outbound_attachment_filename("video", getattr(video, "file_name", None)),
        }

    audio = getattr(message, "audio", None)
    if audio:
        return {
            "kind": "audio",
            "file_id": audio.file_id,
            "filename": outbound_attachment_filename("audio", getattr(audio, "file_name", None)),
        }

    voice = getattr(message, "voice", None)
    if voice:
        return {
            "kind": "voice",
            "file_id": voice.file_id,
            "filename": outbound_attachment_filename("voice", getattr(voice, "file_name", None)),
        }

    video_note = getattr(message, "video_note", None)
    if video_note:
        return {
            "kind": "video_note",
            "file_id": video_note.file_id,
            "filename": outbound_attachment_filename("video_note", getattr(video_note, "file_name", None)),
        }

    sticker = getattr(message, "sticker", None)
    if sticker:
        is_animated = bool(getattr(sticker, "is_animated", False))
        is_video = bool(getattr(sticker, "is_video", False))
        return {
            "kind": "sticker",
            "file_id": sticker.file_id,
            "filename": outbound_attachment_filename(
                "sticker",
                getattr(sticker, "file_name", None),
                is_animated=is_animated,
                is_video=is_video,
            ),
        }

    return None


async def download_outbound_attachment(bot: Bot, attachment: dict | None) -> bytes | None:
    if not attachment or not attachment.get("file_id"):
        return None
    buffer = BytesIO()
    await bot.download(attachment["file_id"], destination=buffer)
    return buffer.getvalue()


async def send_client_bot_payload(
    bot: Bot,
    chat_id: int,
    *,
    text: str,
    attachment: dict | None = None,
    attachment_bytes: bytes | None = None,
    local_photo_path: Path | None = None,
) -> None:
    kind = attachment.get("kind") if attachment else None
    if local_photo_path is not None:
        await bot.send_photo(chat_id, photo=FSInputFile(str(local_photo_path)), caption=text or None)
        return
    if attachment is None:
        await bot.send_message(chat_id, text)
        return
    filename = attachment.get("filename") or "attachment.bin"
    media = BufferedInputFile(attachment_bytes or b"", filename=filename)
    if kind == "photo":
        await bot.send_photo(chat_id, photo=media, caption=text or None)
        return
    if kind == "animation":
        await bot.send_animation(chat_id, animation=media, caption=text or None)
        return
    if kind == "document":
        await bot.send_document(chat_id, document=media, caption=text or None)
        return
    if kind == "video":
        await bot.send_video(chat_id, video=media, caption=text or None)
        return
    if kind == "audio":
        await bot.send_audio(chat_id, audio=media, caption=text or None)
        return
    if kind == "voice":
        await bot.send_voice(chat_id, voice=media, caption=text or None)
        return
    if kind == "video_note":
        await bot.send_video_note(chat_id, video_note=media)
        if text:
            await bot.send_message(chat_id, text)
        return
    if kind == "sticker":
        await bot.send_sticker(chat_id, sticker=media)
        if text:
            await bot.send_message(chat_id, text)
        return
    raise ValueError(f"Unsupported attachment kind: {kind}")


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
    return await download_outbound_attachment(
        callback.bot,
        {
            "kind": "photo",
            "file_id": file_id,
            "filename": outbound_attachment_filename("photo"),
        },
    )


async def show_broadcast_preview(
    target: Message | CallbackQuery,
    state: FSMContext,
    *,
    use_default_logo: bool,
) -> None:
    data = await state.get_data()
    text = (data.get("broadcast_text") or "").strip()
    attachment = data.get("broadcast_attachment")
    file_id = data.get("broadcast_file_id")
    if attachment is None and file_id:
        attachment = {"kind": "photo", "file_id": file_id, "filename": outbound_attachment_filename("photo")}
    attachment_label = (
        f"кастомное: {outbound_attachment_label(attachment.get('kind'))}"
        if attachment
        else "логотип по умолчанию"
        if use_default_logo
        else "нет"
    )
    preview_text = "Предпросмотр рассылки\n\n"
    if text:
        preview_text += f"{text}\n\n"
    preview_text += f"Вложение: {attachment_label}"
    await render_admin(target, preview_text, reply_markup=broadcast_preview_actions().as_markup(), force_new=True)


@router.message(CommandStart())
async def start(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        await message.answer("Доступ к admin bot запрещён.")
        return
    clear_admin_mode(message.from_user.id)
    await message.answer(
        "admin altlink bot готов к работе.\n\n"
        "Здесь можно искать пользователей, смотреть их подписки, баланс, online-сессии, логи, серверы, промокоды, бэкапы базы и рассылки.",
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
        "Карточка пользователя: подписка, баланс, online, удаление аккаунта и личные сообщения с любыми вложениями.\n"
        "Промокоды: создание и быстрый контроль активности.\n"
        "Рассылка: сообщение всем пользователям с текстом, файлами, медиа, gif, голосовыми, кружками и стикерами.\n"
        "Логи: последние системные события приложения.\n"
        "База данных: экспорт JSON backup и импорт с подтверждением через админ-бота.\n"
        "Техработы: ручное отключение клиентского бота и список исключений для тестовых пользователей.\n"
        "Если нужно быстро найти пользователя, просто нажмите «Пользователи» и отправьте ID, username или UUID.",
    )


@router.message(F.text == "База данных")
async def database_backup_menu(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    await database_backup_screen(message, state, container)


@router.message(F.text == "Техработы")
async def maintenance_menu(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    await maintenance_screen(message, state, container)


@router.message(F.text == "Платежи")
async def payments_screen(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    await render_payment_browser(message, container)


@router.callback_query(F.data.startswith(f"{PAYMENT_PAGE_PREFIX}:"))
async def payment_page(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    try:
        index = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Не удалось открыть платёж.", show_alert=True)
        return
    await render_payment_browser(callback, container, index=index)


@router.callback_query(F.data.startswith(f"{PAYMENT_REFRESH_PREFIX}:"))
async def payment_refresh(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    request_id = callback.data.split(":")[-1]
    await render_payment_browser(callback, container, request_id=request_id)


@router.callback_query(F.data.startswith(f"{PAYMENT_APPROVE_PREFIX}:"))
async def payment_approve(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    request_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        item = await hub.topups.get_request(request_id)
        if payment_provider_code(item) != "manual":
            await callback.answer(
                "??? ?????? ??? ?????? ????????????? ?? ?????.",
                show_alert=True,
            )
            return
        admin = await hub.accounts.get_admin_by_telegram_id(callback.from_user.id)
        await hub.topups.approve(
            request_id,
            admin_id=admin.id if admin else None,
            comment="???????????? ? admin bot",
        )
    await render_payment_browser(callback, container, request_id=request_id)


@router.callback_query(F.data.startswith(f"{PAYMENT_REJECT_PREFIX}:"))
async def payment_reject(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    request_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        item = await hub.topups.get_request(request_id)
        if payment_provider_code(item) != "manual":
            await callback.answer(
                "??? ?????? ??? ?????? ?????????? ?? ?????.",
                show_alert=True,
            )
            return
        admin = await hub.accounts.get_admin_by_telegram_id(callback.from_user.id)
        await hub.topups.reject(
            request_id,
            admin_id=admin.id if admin else None,
            comment="????????? ? admin bot",
        )
    await render_payment_browser(callback, container, request_id=request_id)


@router.message(F.text == "????????????")
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


@router.callback_query(F.data.startswith(f"{USER_MESSAGE_PREFIX}:"))
async def open_user_direct_message(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    if not container.settings.client_bot_token:
        await callback.answer("CLIENT_BOT_TOKEN не задан.", show_alert=True)
        return
    user_id = callback.data.split(":")[-1]
    await state.set_state(DirectMessageStates.waiting_for_text)
    await state.update_data(direct_message_user_id=user_id)
    await show_direct_message_prompt(callback, user_id, container)


@router.callback_query(F.data.startswith(f"{USER_MESSAGE_CANCEL_PREFIX}:"))
async def cancel_user_direct_message(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await state.clear()
    await show_user_card(callback, callback.data.split(":")[-1], container)


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


@router.message(DirectMessageStates.waiting_for_text)
async def user_direct_message_submit(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    if await route_admin_menu_action(message, state, container):
        return

    data = await state.get_data()
    user_id = data.get("direct_message_user_id")
    if not user_id:
        await state.clear()
        await render_admin(message, "Сценарий личного сообщения потерян. Откройте карточку пользователя заново.")
        return

    text = outbound_message_text(message)
    attachment = extract_outbound_attachment(message)
    if not text and attachment is None:
        await render_admin(
            message,
            "Отправьте текст, файл, медиа, gif, голосовое, кружок или стикер.",
            reply_markup=user_message_prompt_actions(user_id).as_markup(),
        )
        return
    if not container.settings.client_bot_token:
        await state.clear()
        await render_admin(message, "CLIENT_BOT_TOKEN не задан. Личное сообщение отправить нельзя.")
        return

    async with container.hub() as hub:
        user = await hub.accounts.get_user(user_id)
        admin = await hub.accounts.get_admin_by_telegram_id(message.from_user.id)
        client_bot = Bot(token=container.settings.client_bot_token)
        try:
            attachment_bytes = await download_outbound_attachment(message.bot, attachment) if attachment else None
            await send_client_bot_payload(
                client_bot,
                user.telegram_id,
                text=text,
                attachment=attachment,
                attachment_bytes=attachment_bytes,
            )
        except Exception as exc:  # noqa: BLE001
            reason = broadcast_failure_reason(exc)
            await render_admin(
                message,
                "Не удалось отправить личное сообщение.\n\n"
                f"Пользователь: {user_label(user)}\n"
                f"Telegram ID: {user.telegram_id}\n"
                f"Причина: {reason}",
                reply_markup=user_message_prompt_actions(user_id).as_markup(),
            )
            return
        finally:
            await client_bot.session.close()

        await hub.accounts.log_event(
            level=SystemEventLevel.INFO,
            event_type="direct_message_sent",
            message="Администратор отправил личное сообщение пользователю.",
            payload={
                "user_id": user.id,
                "telegram_id": user.telegram_id,
                "text_preview": compact_text(text, 120) if text else None,
                "attachment_kind": attachment.get("kind") if attachment else None,
            },
            actor_admin_id=admin.id if admin else None,
        )

    await state.clear()
    await render_admin(
        message,
        "Личное сообщение отправлено.\n\n"
        f"Пользователь: {user_label(user)}\n"
        f"Telegram ID: {user.telegram_id}\n"
        f"Текст: {compact_text(text, 220)}",
        reply_markup=user_actions(user_id).as_markup(),
    )


@router.message(F.text == "Серверы")
async def servers_screen(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        await sync_server_catalog_if_possible(hub)
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
        try:
            server = await hub.catalog.set_server_availability(server_id, flag == "1")
        except NotFoundError:
            await render_admin(callback, "Сервер уже удалён из локальной базы. Синхронизируйте список заново.")
            return
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
        try:
            server = await hub.catalog.set_server_type(server_id, ServerType(raw_type))
        except NotFoundError:
            await render_admin(callback, "Сервер уже удалён из локальной базы. Синхронизируйте список заново.")
            return
    await render_admin(
        callback,
        format_server_card(server),
        reply_markup=server_actions(
            getattr(server, "id", server_id),
            getattr(server, "is_available", True),
        ).as_markup(),
    )


@router.callback_query(F.data.startswith(f"{SERVER_OPEN_PREFIX}:"))
async def open_server_card(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    server_id = callback.data.split(":", 2)[2]
    async with container.hub() as hub:
        try:
            server = await hub.catalog.get_server(server_id)
        except NotFoundError:
            await render_admin(callback, "Сервер уже отсутствует в локальной базе.")
            return
    await render_admin(
        callback,
        format_server_card(server),
        reply_markup=server_actions(server.id, server.is_available).as_markup(),
    )


@router.callback_query(F.data.startswith(f"{SERVER_DELETE_PREFIX}:"))
async def confirm_force_delete_server(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    server_id = callback.data.split(":", 2)[2]
    async with container.hub() as hub:
        try:
            server = await hub.catalog.get_server(server_id)
        except NotFoundError:
            await render_admin(callback, "Сервер уже отсутствует в локальной базе.")
            return
    await render_admin(
        callback,
        "Удалить сервер из локальной базы?\n\n"
        f"{format_server_card(server)}\n\n"
        "Это не удаляет ноду или squad в Remnawave. Если нода всё ещё есть в Remnawave, "
        "следующая синхронизация добавит её в базу снова.",
        reply_markup=server_delete_confirmation_actions(server_id).as_markup(),
    )


@router.callback_query(F.data.startswith(f"{SERVER_DELETE_CONFIRM_PREFIX}:"))
async def force_delete_server(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    server_id = callback.data.split(":", 2)[2]
    async with container.hub() as hub:
        try:
            summary = await hub.catalog.force_delete_server(server_id)
        except NotFoundError:
            await render_admin(callback, "Сервер уже отсутствует в локальной базе.")
            return
    await render_admin(
        callback,
        "Сервер удалён из локальной базы.\n\n"
        f"Название: {summary['name']}\n"
        f"Адрес: {summary['address']}\n"
        f"Сброшено назначений: {summary['assigned_users']}\n"
        f"Удалено доступов: {summary['accesses']}\n"
        f"Удалено inbound'ов: {summary['inbounds']}\n\n"
        "Если нода осталась в Remnawave, при следующей синхронизации она появится снова.",
    )


@router.message(F.text == "Аналитика")
async def statistics(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    await sync_dashboard_traffic_if_possible(container)
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
    await sync_dashboard_traffic_if_possible(container)
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
    await sync_dashboard_traffic_if_possible(container)
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
    await state.update_data(
        broadcast_text=None,
        broadcast_file_id=None,
        broadcast_attachment=None,
        broadcast_use_default=False,
    )
    await render_admin(
        message,
        "Рассылка\n\n"
        "Сначала отправьте текст сообщения.\n"
        "Либо сразу отправьте файл, медиа, gif, голосовое, кружок или стикер. "
        "Текст тогда возьмётся из подписи, если она есть.",
    )


@router.message(BroadcastStates.waiting_for_text)
async def broadcast_text_submit(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    if await route_admin_menu_action(message, state, container):
        return
    text = outbound_message_text(message)
    attachment = extract_outbound_attachment(message)
    if attachment is not None:
        await state.update_data(
            broadcast_text=text,
            broadcast_file_id=attachment["file_id"] if attachment.get("kind") == "photo" else None,
            broadcast_attachment=attachment,
            broadcast_use_default=False,
        )
        await state.set_state(BroadcastStates.waiting_for_media)
        await show_broadcast_preview(message, state, use_default_logo=False)
        return
    if not text:
        await render_admin(message, "Текст рассылки не должен быть пустым.")
        return
    await state.update_data(broadcast_text=text, broadcast_attachment=None, broadcast_file_id=None)
    await state.set_state(BroadcastStates.waiting_for_media)
    await render_admin(
        message,
        "Текст рассылки сохранён.\n\n"
        "Теперь отправьте файл, медиа, gif, голосовое, кружок или стикер.\n"
        "Если вложение не нужно, нажмите «Без вложения». Для фото-рассылки с фирменной картинкой можно выбрать логотип сервиса.",
        reply_markup=broadcast_media_actions().as_markup(),
    )


@router.callback_query(F.data == "admin:broadcast:default")
async def broadcast_use_default(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await state.update_data(broadcast_use_default=True, broadcast_file_id=None, broadcast_attachment=None)
    await show_broadcast_preview(callback, state, use_default_logo=True)


@router.callback_query(F.data == "admin:broadcast:text_only")
async def broadcast_text_only(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await state.update_data(broadcast_use_default=False, broadcast_file_id=None, broadcast_attachment=None)
    await show_broadcast_preview(callback, state, use_default_logo=False)


@router.callback_query(F.data == "admin:broadcast:cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await state.clear()
    await render_admin(callback, "Создание рассылки отменено.")


@router.message(BroadcastStates.waiting_for_media)
async def broadcast_media_submit(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    if await route_admin_menu_action(message, state, container):
        return
    attachment = extract_outbound_attachment(message)
    if attachment is None:
        await render_admin(
            message,
            "Ожидается файл, медиа, gif, голосовое, кружок или стикер. Если вложение не нужно, нажмите кнопку ниже.",
            reply_markup=broadcast_media_actions().as_markup(),
        )
        return
    await state.update_data(
        broadcast_file_id=attachment["file_id"] if attachment.get("kind") == "photo" else None,
        broadcast_attachment=attachment,
        broadcast_use_default=False,
    )
    await show_broadcast_preview(message, state, use_default_logo=False)


@router.callback_query(F.data == "admin:broadcast:confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    if not container.settings.client_bot_token:
        await callback.answer("CLIENT_BOT_TOKEN не задан.", show_alert=True)
        return
    data = await state.get_data()
    text = (data.get("broadcast_text") or "").strip()
    attachment = data.get("broadcast_attachment")
    file_id = data.get("broadcast_file_id")
    use_default_logo = bool(data.get("broadcast_use_default"))
    if attachment is None and file_id:
        attachment = {
            "kind": "photo",
            "file_id": file_id,
            "filename": f"broadcast-{datetime.now(UTC):%Y%m%d%H%M%S}.jpg",
        }
    if not text and attachment is None and not use_default_logo:
        await callback.answer("Нет ни текста, ни вложения для рассылки. Начните заново.", show_alert=True)
        await state.clear()
        return

    attachment_bytes = await download_outbound_attachment(callback.bot, attachment) if attachment else None
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
                    await send_client_bot_payload(
                        client_bot,
                        user.telegram_id,
                        text=text,
                        attachment=attachment,
                        attachment_bytes=attachment_bytes,
                        local_photo_path=logo,
                    )
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
                "attachment_kind": attachment.get("kind") if attachment else ("photo" if logo is not None else None),
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


@router.callback_query(F.data == DATABASE_BACKUP_OPEN)
async def database_backup_open(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await database_backup_screen(callback, state, container)


@router.callback_query(F.data == MAINTENANCE_OPEN)
async def maintenance_open(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await maintenance_screen(callback, state, container)


@router.callback_query(F.data == MAINTENANCE_TOGGLE)
async def maintenance_toggle(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(callback.from_user.id)
        current = await hub.monitoring.get_manual_client_maintenance_state()
        updated = await hub.monitoring.set_manual_client_maintenance(
            not bool(current.get("enabled")),
            actor_admin_id=admin.id if admin else None,
        )
    await render_manual_maintenance_screen(callback, container=container, manual_state=updated)


@router.callback_query(F.data == MAINTENANCE_ADD_EXCEPTION)
async def maintenance_add_exception_prompt(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await state.set_state(MaintenanceStates.waiting_for_add_exception_query)
    await render_admin(
        callback,
        maintenance_exception_prompt_text(),
        reply_markup=maintenance_prompt_actions().as_markup(),
    )


@router.callback_query(F.data == MAINTENANCE_CANCEL)
async def maintenance_cancel(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    await maintenance_screen(callback, state, container)


@router.callback_query(F.data.startswith(f"{MAINTENANCE_PICK_ADD_PREFIX}:"))
async def maintenance_pick_add_exception(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    user_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(callback.from_user.id)
        try:
            user = await hub.accounts.get_user(user_id)
        except NotFoundError:
            await callback.answer("Пользователь не найден, попробуйте поиск ещё раз.", show_alert=True)
            return
        updated = await hub.monitoring.add_manual_maintenance_exception(
            user,
            actor_admin_id=admin.id if admin else None,
        )
    await state.clear()
    await render_manual_maintenance_screen(callback, container=container, manual_state=updated)


@router.callback_query(F.data.startswith(f"{MAINTENANCE_REMOVE_PREFIX}:"))
async def maintenance_remove_exception(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    user_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(callback.from_user.id)
        current = await hub.monitoring.get_manual_client_maintenance_state()
        item = next((entry for entry in current.get("exceptions", []) if str(entry.get("user_id")) == user_id), None)
        if item is None:
            await callback.answer("Исключение уже удалено.", show_alert=True)
            return
        try:
            user = await hub.accounts.get_user(user_id)
        except NotFoundError:
            user = SimpleNamespace(
                id=user_id,
                telegram_id=item.get("telegram_id"),
                username=item.get("username"),
            )
        updated = await hub.monitoring.remove_manual_maintenance_exception(
            user,
            actor_admin_id=admin.id if admin else None,
        )
    await render_manual_maintenance_screen(callback, container=container, manual_state=updated)


@router.callback_query(F.data == DATABASE_BACKUP_EXPORT)
async def database_backup_export(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    async with container.hub() as hub:
        artifact = await hub.backups.export_database()
    sent = await callback.message.answer_document(
        BufferedInputFile(artifact.content, filename=artifact.filename),
        caption=format_database_backup_summary(artifact.summary, title="Экспорт базы готов"),
    )
    remember_admin_card(sent)
    await callback.answer("Резервная копия отправлена.")


@router.callback_query(F.data == DATABASE_BACKUP_IMPORT)
async def database_backup_import_prompt(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    clear_pending_database_import(callback.from_user.id)
    await state.set_state(DatabaseImportStates.waiting_for_document)
    await render_admin(
        callback,
        "Импорт базы данных\n\nОтправьте сюда JSON backup-файл. После проверки бот покажет сводку и попросит подтверждение.\n\nТекущая база пока не изменяется.",
        reply_markup=database_import_prompt_actions().as_markup(),
    )


@router.message(DatabaseImportStates.waiting_for_document, F.document)
async def database_backup_import_document(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    buffer = BytesIO()
    await message.bot.download(message.document, destination=buffer)
    payload = buffer.getvalue()
    async with container.hub() as hub:
        try:
            summary = await hub.backups.inspect_database_backup(payload)
        except ServiceError as exc:
            await render_admin(
                message,
                f"{exc}\n\nОтправьте корректный JSON backup-файл или нажмите «Назад».",
                reply_markup=database_import_prompt_actions().as_markup(),
            )
            return
    set_pending_database_import(message.from_user.id, payload)
    await state.set_state(DatabaseImportStates.waiting_for_confirmation)
    await state.update_data(database_import_summary=summary)
    await render_admin(
        message,
        format_database_import_confirmation(summary),
        reply_markup=database_import_confirmation_actions().as_markup(),
    )


@router.message(DatabaseImportStates.waiting_for_document)
async def database_backup_import_document_fallback(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    if await route_admin_menu_action(message, state, container):
        return
    await render_admin(
        message,
        "Ожидается JSON backup-файл Telegram-документом. Текущая база пока не изменяется.",
        reply_markup=database_import_prompt_actions().as_markup(),
    )


@router.callback_query(F.data == DATABASE_BACKUP_CONFIRM_IMPORT)
async def database_backup_import_confirm(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    payload = get_pending_database_import(callback.from_user.id)
    if payload is None:
        await state.clear()
        await callback.answer("Файл резервной копии не найден, загрузите его заново.", show_alert=True)
        await render_admin(
            callback,
            "Импорт нужно начать заново: отправьте backup-файл ещё раз.",
            reply_markup=database_import_prompt_actions().as_markup(),
        )
        await state.set_state(DatabaseImportStates.waiting_for_document)
        return
    async with container.hub() as hub:
        try:
            summary = await hub.backups.import_database(payload)
        except ServiceError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
    clear_pending_database_import(callback.from_user.id)
    await state.clear()
    await render_admin(
        callback,
        format_database_backup_summary(summary, title="Импорт базы завершён")
        + "\n\nТекущая локальная база заменена данными из backup-файла.",
        reply_markup=database_backup_actions().as_markup(),
    )


@router.callback_query(F.data == DATABASE_BACKUP_CANCEL_IMPORT)
async def database_backup_import_cancel(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    clear_pending_database_import(callback.from_user.id)
    await database_backup_screen(callback, state, container)


@router.message(DatabaseImportStates.waiting_for_confirmation)
async def database_backup_import_confirmation_fallback(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    if await route_admin_menu_action(message, state, container):
        return
    data = await state.get_data()
    summary = data.get("database_import_summary") or {}
    await render_admin(
        message,
        format_database_import_confirmation(summary) if summary else "Нажмите кнопку подтверждения или отмены импорта ниже.",
        reply_markup=database_import_confirmation_actions().as_markup() if summary else database_import_prompt_actions().as_markup(),
    )


@router.message(MaintenanceStates.waiting_for_add_exception_query)
async def maintenance_add_exception_submit(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    if await route_admin_menu_action(message, state, container):
        return
    query = normalize_user_lookup_query(message.text or "")
    if not query:
        await render_admin(
            message,
            maintenance_exception_prompt_text(),
            reply_markup=maintenance_prompt_actions().as_markup(),
        )
        return

    updated: dict | None = None
    async with container.hub() as hub:
        users = await hub.accounts.list_users(query)
        if not users:
            await render_admin(
                message,
                "Пользователь не найден.\n\n"
                + maintenance_exception_prompt_text(),
                reply_markup=maintenance_prompt_actions().as_markup(),
            )
            return
        if len(users) == 1:
            admin = await hub.accounts.get_admin_by_telegram_id(message.from_user.id)
            updated = await hub.monitoring.add_manual_maintenance_exception(
                users[0],
                actor_admin_id=admin.id if admin else None,
            )
    if updated is not None:
        await state.clear()
        await render_manual_maintenance_screen(message, container=container, manual_state=updated)
        return

    lines = [f"Найдено пользователей: {len(users)}.", "", "Выберите, кого добавить в исключения:"]
    for item in users[:10]:
        lines.append(f"• {user_label(item)} — {item.telegram_id}")
    await render_admin(
        message,
        "\n".join(lines),
        reply_markup=maintenance_user_pick_actions(users[:10]).as_markup(),
    )


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
        DirectMessageStates.waiting_for_text.state,
        PromoStates.waiting_for_payload.state,
        BroadcastStates.waiting_for_text.state,
        BroadcastStates.waiting_for_media.state,
        MaintenanceStates.waiting_for_add_exception_query.state,
    }:
        return

    if admin_mode(message.from_user.id) == "user_search" or is_user_lookup_query(message.text):
        set_admin_mode(message.from_user.id, "user_search")
        await process_user_lookup(message, container)
