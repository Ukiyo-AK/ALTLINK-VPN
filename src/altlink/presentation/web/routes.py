from __future__ import annotations

import asyncio
import logging
import base64
import json
import re
import secrets
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from urllib.parse import unquote, urlparse

from aiogram import Bot
from aiogram.types import FSInputFile
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload

from altlink.domain.billing import bytes_to_gb_cost
from altlink.application.services.accounts import DEFAULT_USER_LIST_PAGE_SIZE, USER_LIST_PAGE_SIZE_OPTIONS, UserListFilters
from altlink.domain.enums import (
    BalanceTransactionType,
    NotificationStatus,
    NotificationType,
    PlanCode,
    PromoRewardKind,
    ServerType,
    SupportRequestStatus,
    TopupStatus,
    TrafficLimitStrategy,
    UserStatus,
)
from altlink.domain.external_api import (
    EXTERNAL_API_RECOMMENDED_SCOPES,
    EXTERNAL_API_SCOPE_DEFINITIONS,
)
from altlink.domain.notifications import (
    PROMO_MESSAGE_TEMPLATES,
    promo_template_kind,
    render_promo_campaign_message,
)
from altlink.domain.plans import (
    WHITELIST_GB_PRICE_RUB,
    WHITELIST_INCLUDED_GB_BY_PLAN,
    WHITELIST_TRAFFIC_PACKAGES,
    is_metered_plan_code,
    parse_paid_plan_code,
)
from altlink.domain.traffic_limits import BYTES_PER_GIB, TRAFFIC_LIMIT_STRATEGY_LABELS
from altlink.infrastructure.db.models import (
    BalanceTransaction,
    Notification,
    PromoCode,
    PromoCodeRedemption,
    Subscription,
    SupportMessage,
    SystemSetting,
    User,
)
from altlink.application.services.billing import DEFAULT_PROMO_CAMPAIGN_SETTINGS, PROMO_CAMPAIGN_SETTINGS_KEY
from altlink.application.services.base import ConflictError, NotFoundError, ServiceError
from altlink.application.services.monitoring import MonitoringService
from altlink.application.services.topups import MIN_TOPUP_AMOUNT_RUB
from altlink.presentation.bots.admin_keyboards import support_request_actions
from altlink.utils.latency import (
    LEGACY_WHITELIST_LATENCY_TARGET_SETTING_KEY,
    LATENCY_RECHECK_THRESHOLD_MS,
    WHITELIST_SERVER_DOMAIN_SETTING_KEY,
    browser_probe_url,
    is_foreign_latency_target,
    normalize_latency_target_domain,
    single_probe_server_latency,
    server_probe_port,
)
from altlink.utils.devices import hwid_device_view
from altlink.utils.qr import render_qr_png
from altlink.utils.security import generate_token
from altlink.utils.telegram_web import (
    check_channel_membership,
    verify_telegram_auth_payload,
    verify_telegram_webapp_init_data,
)
from altlink.utils.time import MOSCOW_TZ, format_msk_date, format_msk_datetime

router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
logger = logging.getLogger(__name__)
DOCUMENT_FILENAMES = {
    "agreement": "altlink_user_agreement.md",
    "privacy": "altlink_privacy_policy.md",
}
DOCUMENT_KEYWORDS = {
    "agreement": ("agreement", "user_agreement", "соглаш"),
    "privacy": ("privacy", "policy", "конфиден", "privacy_policy"),
}
TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
PORTAL_LOGIN_ATTEMPT_SESSION_KEY = "portal_login_attempt_token"
ASSET_VERSION = "20260827-admin-analytics"
COUNTRY_NAMES_RU = {
    "AM": "Армения",
    "AT": "Австрия",
    "BY": "Беларусь",
    "CH": "Швейцария",
    "CZ": "Чехия",
    "DE": "Германия",
    "EE": "Эстония",
    "ES": "Испания",
    "FI": "Финляндия",
    "FR": "Франция",
    "GB": "Великобритания",
    "GE": "Грузия",
    "HK": "Гонконг",
    "JP": "Япония",
    "KZ": "Казахстан",
    "LT": "Литва",
    "LV": "Латвия",
    "NL": "Нидерланды",
    "NO": "Норвегия",
    "PL": "Польша",
    "RU": "Россия",
    "SE": "Швеция",
    "SG": "Сингапур",
    "TR": "Турция",
    "UA": "Украина",
    "US": "США",
}


async def sync_dashboard_traffic_if_possible(container) -> None:
    try:
        async with container.hub() as sync_hub:
            billing = getattr(sync_hub, "billing", None)
            if billing is None:
                return
            await asyncio.wait_for(billing.snapshot_traffic(), timeout=8)
    except TimeoutError:
        logger.warning("Timed out while syncing traffic snapshots before web admin render.")
    except Exception:
        logger.warning("Failed to sync traffic snapshots before web admin render.", exc_info=True)


async def sync_server_catalog_if_possible(hub) -> None:
    sync_method = getattr(getattr(hub, "catalog", None), "sync_servers", None)
    if sync_method is None:
        return
    try:
        await sync_method()
    except Exception:
        logger.warning("Failed to sync server catalog before web admin render.", exc_info=True)
        session = getattr(hub, "session", None)
        rollback = getattr(session, "rollback", None)
        if rollback is not None:
            await rollback()


def latency_target_label() -> str:
    return "вашего устройства до нод ALTLINK"


def latency_disclaimer_text() -> str:
    return (
        "Показываем задержку от вашего устройства до серверов ALTLINK. "
        "Это помогает точнее оценить подключение, но значение может немного меняться из-за вашей сети и маршрута."
    )


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = generate_token(16)
        request.session["csrf_token"] = token
    return token


def validate_csrf(request: Request, form: dict) -> None:
    if form.get("csrf_token") != request.session.get("csrf_token"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Некорректный CSRF токен.")


def set_flash(request: Request, message: str, level: str = "success") -> None:
    request.session["flash"] = {"message": message, "level": level}


def parse_decimal_query(value: str | None) -> Decimal | None:
    if value is None or not str(value).strip():
        return None
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except Exception:
        return None


def parse_int_query(value: str | None) -> int | None:
    if value is None or not str(value).strip():
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def parse_gb_to_bytes(value: str | None) -> int | None:
    amount = parse_decimal_query(value)
    if amount is None:
        return None
    return max(int(amount * Decimal(1024**3)), 0)


def parse_date_query(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    edge = time.max if end_of_day else time.min
    return datetime.combine(parsed, edge, tzinfo=MOSCOW_TZ).astimezone(UTC)


def parse_msk_datetime_input(value: str | None) -> datetime | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ConflictError("Некорректный срок действия API-ключа.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    return parsed.astimezone(UTC)


def normalize_user_list_limit(value: int | None) -> int:
    return value if value in USER_LIST_PAGE_SIZE_OPTIONS else DEFAULT_USER_LIST_PAGE_SIZE


async def load_server_latency_state(session) -> tuple[dict[str, dict], str | None]:
    item = await session.scalar(select(SystemSetting).where(SystemSetting.key == MonitoringService.SERVER_LATENCY_STATUS_KEY))
    raw = item.value if item is not None else None
    if not isinstance(raw, dict):
        return {}, None
    servers = raw.get("servers", {})
    checked_at = raw.get("checked_at")
    return (servers if isinstance(servers, dict) else {}), (checked_at if isinstance(checked_at, str) else None)


def landing_latency_label(latency: dict | None) -> tuple[str, str]:
    if not isinstance(latency, dict):
        return "Не измерен", "muted"
    if latency.get("reachable"):
        latency_ms = latency.get("latency_ms")
        if isinstance(latency_ms, int | float):
            return f"{round(latency_ms)} мс", "ready"
        return "Нет данных", "muted"
    return "Недоступно", "error"


def country_flag(country_code: str | None) -> str:
    code = (country_code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return "🌐"
    return "".join(chr(ord(char) - ord("A") + 0x1F1E6) for char in code)


def country_name(country_code: str | None) -> str:
    code = (country_code or "").strip().upper()
    return COUNTRY_NAMES_RU.get(code) or code or "Локация"


def format_rub_amount(value) -> str:
    amount = Decimal(str(value))
    if amount == amount.to_integral_value():
        return str(int(amount))
    return f"{amount.normalize():f}".rstrip("0").rstrip(".")


def user_status_label(value) -> str:
    status = getattr(value, "value", value)
    return {
        "new": "Новый",
        "trial": "Тест",
        "active": "Активен",
        "grace": "Льготный период",
        "blocked": "Заблокирован",
        "canceled": "Отменён",
        "deleted": "Удалён",
    }.get(str(status), str(status))


def payment_status_label(value) -> str:
    status = getattr(value, "value", value)
    raw_status = str(status)
    if raw_status in {
        "Зачислено",
        "Отклонён",
        "Отменён",
        "Новый",
        "Оплачен",
        "Ожидает оплаты",
        "Ожидает подтверждения",
        "Истёк",
    }:
        return raw_status
    return {
        "approved": "Зачислено",
        "new": "Новый",
        "succeeded": "Оплачен",
        "paid": "Оплачен",
        "pending": "Ожидает оплаты",
        "waiting_for_capture": "Ожидает подтверждения",
        "canceled": "Отменён",
        "cancelled": "Отменён",
        "rejected": "Отклонён",
        "expired": "Истёк",
    }.get(raw_status, "Неизвестный статус")


def balance_transaction_type_label(value) -> str:
    transaction_type = getattr(value, "value", value)
    return {
        "topup": "Пополнение",
        "subscription_charge": "Оплата подписки",
        "manual_adjustment": "Ручная корректировка",
        "refund": "Возврат",
        "promo_applied": "Применение промокода",
        "promo_bonus": "Бонус по промокоду",
        "referral_bonus": "Реферальный бонус",
    }.get(str(transaction_type), str(transaction_type))


def normalize_promo_campaign_settings(value: object) -> dict[str, int | bool]:
    raw = value if isinstance(value, dict) else {}
    settings = dict(DEFAULT_PROMO_CAMPAIGN_SETTINGS)
    settings.update({key: raw[key] for key in settings if key in raw})

    def bounded_int(key: str, minimum: int, maximum: int, fallback: int) -> int:
        try:
            parsed = int(settings.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
        return min(max(parsed, minimum), maximum)

    return {
        "new_user_discount_percent": bounded_int("new_user_discount_percent", 1, 100, 10),
        "lapsed_user_discount_percent": bounded_int("lapsed_user_discount_percent", 1, 100, 35),
        "inactive_first_delay_days": bounded_int("inactive_first_delay_days", 1, 365, 3),
        "lapsed_first_delay_days": bounded_int("lapsed_first_delay_days", 1, 365, 1),
        "deep_winback_delay_days": bounded_int("deep_winback_delay_days", 1, 365, 30),
        "return_trial_enabled": bool(settings.get("return_trial_enabled", True)),
        "return_trial_cooldown_days": bounded_int("return_trial_cooldown_days", 1, 365, 30),
    }


def promo_template_options() -> list[dict[str, object]]:
    return [
        {
            "id": template_id,
            "kind": template["kind"],
            "label": f"{template_id}. {'Скидка' if template['kind'] == 'discount' else 'Тест'}",
        }
        for template_id, template in PROMO_MESSAGE_TEMPLATES.items()
    ]


def promo_template_preview(template_id: int, *, discount_percent: int = 10, trial_days: int = 2) -> str:
    kind = promo_template_kind(template_id)
    promo_code = "ABCDEFGH" if kind == "discount" else None
    return render_promo_campaign_message(
        template_id,
        promo_code=promo_code,
        discount_percent=discount_percent,
        trial_days=trial_days,
    )


def promo_notification_kind(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return "other"
    if payload.get("cta") == "return_trial":
        return "Повторный тест"
    campaign_kind = payload.get("campaign_kind")
    if campaign_kind == "lapsed_fresh":
        return "Бывшие платные · 10%"
    if campaign_kind == "lapsed_deep":
        return "Бывшие платные · 35%"
    if campaign_kind == "new_fresh":
        return "Новые/без оплаты"
    if campaign_kind == "new_deep":
        return "Давний trial · 35%"
    if payload.get("cta") == "trial_followup":
        return "Свежий trial · 10%"
    return "Другое"


async def promo_admin_stats(session) -> dict[str, object]:
    notifications = list(
        (
            await session.scalars(
                select(Notification)
                .where(Notification.payload.is_not(None))
                .order_by(Notification.created_at.desc())
                .limit(5000)
            )
        ).all()
    )
    promo_notifications = [
        item
        for item in notifications
        if isinstance(item.payload, dict)
        and item.payload.get("cta") in {"inactive_promo", "trial_followup", "return_trial"}
    ]
    redemptions = list(
        (
            await session.scalars(
                select(PromoCodeRedemption)
                .join(PromoCode)
                .options(joinedload(PromoCodeRedemption.promo_code))
                .where(PromoCode.reward_kind == PromoRewardKind.PLAN_DISCOUNT)
                .order_by(PromoCodeRedemption.created_at.desc())
                .limit(5000)
            )
        ).all()
    )
    codes = list((await session.scalars(select(PromoCode))).all())

    sent_by_kind: dict[str, int] = {}
    sent_by_discount: dict[str, int] = {}
    sent_by_template: dict[str, int] = {}
    for item in promo_notifications:
        payload = item.payload if isinstance(item.payload, dict) else {}
        kind = promo_notification_kind(payload)
        sent_by_kind[kind] = sent_by_kind.get(kind, 0) + 1
        template_id = payload.get("template_id")
        if template_id:
            label = f"#{template_id}"
            sent_by_template[label] = sent_by_template.get(label, 0) + 1
        discount = payload.get("discount_percent")
        if discount:
            label = f"{discount}%"
            sent_by_discount[label] = sent_by_discount.get(label, 0) + 1

    activated_by_discount: dict[str, int] = {}
    applied_by_discount: dict[str, int] = {}
    for redemption in redemptions:
        promo = redemption.promo_code
        if promo is None:
            continue
        label = f"{format_rub_amount(promo.reward_value)}%"
        activated_by_discount[label] = activated_by_discount.get(label, 0) + 1
        if redemption.applied_at is not None:
            applied_by_discount[label] = applied_by_discount.get(label, 0) + 1

    def chart_from(mapping: dict[str, int]) -> dict[str, list]:
        return {"labels": list(mapping.keys()), "values": list(mapping.values())}

    return {
        "sent_total": len(promo_notifications),
        "sent_pending": len([item for item in promo_notifications if item.status == NotificationStatus.PENDING]),
        "sent_success": len([item for item in promo_notifications if item.status == NotificationStatus.SENT]),
        "sent_failed": len([item for item in promo_notifications if item.status == NotificationStatus.FAILED]),
        "activated_total": len(redemptions),
        "applied_total": len([item for item in redemptions if item.applied_at is not None]),
        "manual_codes": len([item for item in codes if item.assigned_user_id is None]),
        "personal_codes": len([item for item in codes if item.assigned_user_id is not None]),
        "return_trial_sent": sent_by_kind.get("Повторный тест", 0),
        "charts": {
            "sent_by_kind": chart_from(sent_by_kind),
            "sent_by_discount": chart_from(sent_by_discount),
            "sent_by_template": chart_from(sent_by_template),
            "activated_by_discount": chart_from(activated_by_discount),
            "applied_by_discount": chart_from(applied_by_discount),
        },
    }


def access_status_label(value) -> str:
    status = getattr(value, "value", value)
    return {
        "active": "Активен",
        "inactive": "Неактивен",
        "disabled": "Отключён",
        "maintenance": "Обслуживание",
    }.get(str(status), "Статус неизвестен")


def support_status_label(item) -> str:
    if getattr(item, "status", None) == SupportRequestStatus.RESOLVED:
        return "Закрыт"
    messages = list(getattr(item, "messages", []) or [])
    if messages and getattr(messages[-1], "sender_type", "") == "admin":
        return "Ожидает вашего ответа"
    return "Ожидает ответа"


def vless_keys_file_content(keys: list[str]) -> bytes:
    lines = [
        "ALTLINK VLESS keys",
        "",
        "Один ключ соответствует одному доступному серверу.",
        "Импортируйте нужную строку vless:// в совместимое приложение вручную.",
        "",
    ]
    for index, key in enumerate(keys, start=1):
        config_name = unquote(urlparse(key).fragment).strip() or f"Конфиг {index}"
        lines.extend([f"{index}. Название конфига: {config_name}", f"Ключ: {key}", ""])
    return "\n".join(lines).encode("utf-8")


templates.env.filters["rub"] = format_rub_amount
templates.env.filters["user_status"] = user_status_label
templates.env.filters["payment_status"] = payment_status_label
templates.env.filters["transaction_type"] = balance_transaction_type_label
templates.env.filters["access_status"] = access_status_label
templates.env.filters["support_status"] = support_status_label
templates.env.filters["msk_datetime"] = format_msk_datetime
templates.env.filters["msk_short_datetime"] = lambda value: format_msk_datetime(value, "%d.%m %H:%M")
templates.env.filters["msk_date"] = format_msk_date


def latency_quality_label(latency_ms) -> str:
    if not isinstance(latency_ms, int | float):
        return "Проверьте пинг"
    if latency_ms <= 30:
        return "Лучший отклик"
    if latency_ms <= 70:
        return "Быстрое соединение"
    if latency_ms <= 120:
        return "Стабильное соединение"
    if latency_ms <= 200:
        return "Подходит для повседневных задач"
    if latency_ms <= 300:
        return "Дальняя локация"
    return "Временно высокая задержка"


def build_landing_location_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    locations: dict[str, dict[str, object]] = {}

    for item in items:
        code = str(item.get("country_code") or "").upper()
        key = code or str(item.get("server_id") or item.get("name") or "")
        if not key:
            continue

        latency_ms = item.get("latency_ms")
        current = locations.get(key)
        should_replace = current is None
        if current is not None and isinstance(latency_ms, int | float):
            current_latency = current.get("latency_ms")
            should_replace = not isinstance(current_latency, int | float) or latency_ms < current_latency

        if should_replace:
            locations[key] = {
                "country_code": code,
                "country_name": str(item.get("country_name") or country_name(code)),
                "country_flag": str(item.get("country_flag") or country_flag(code)),
                "latency_ms": latency_ms if isinstance(latency_ms, int | float) else None,
                "display_label": item.get("display_label") or "Не измерен",
                "display_state": item.get("display_state") or "muted",
                "quality_label": latency_quality_label(latency_ms),
                "server_count": 1 if current is None else int(current.get("server_count", 1)) + 1,
            }
        else:
            locations[key]["server_count"] = int(locations[key].get("server_count", 1)) + 1

    return sorted(
        locations.values(),
        key=lambda item: (
            item.get("country_name", ""),
            item.get("country_code", ""),
        ),
    )


def build_landing_latency_items(servers, latency_state: dict[str, dict]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for server in servers or []:
        if not getattr(server, "is_available", False):
            continue
        latency = latency_state.get(getattr(server, "id", "")) if isinstance(latency_state, dict) else None
        label, state = landing_latency_label(latency)
        latency_ms = latency.get("latency_ms") if isinstance(latency, dict) else None
        country_code = (getattr(server, "country_code", "") or "").upper()
        items.append(
            {
                "server_id": getattr(server, "id", None),
                "name": getattr(server, "name", None),
                "country_code": country_code,
                "country_name": country_name(country_code),
                "country_flag": country_flag(country_code),
                "reachable": bool(latency.get("reachable")) if isinstance(latency, dict) else False,
                "latency_ms": latency_ms if isinstance(latency_ms, int | float) else None,
                "display_label": label,
                "display_state": state,
                "probe_target_host": latency.get("probe_target_host") if isinstance(latency, dict) else None,
                "checked_at": latency.get("checked_at") if isinstance(latency, dict) else None,
            }
        )
    return sorted(
        [item for item in items if item.get("server_id")],
        key=lambda item: (
            item.get("country_code", ""),
            item.get("name", ""),
        ),
    )


def pop_flash(request: Request) -> dict | None:
    return request.session.pop("flash", None)


def render(request: Request, template_name: str, **context):
    response = templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "asset_version": ASSET_VERSION,
            "csrf_token": get_csrf_token(request),
            "flash": pop_flash(request),
            **context,
        },
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def format_user_node_access_sync_flash(summary: dict[str, object]) -> str:
    parts = [
        f"проверено {summary.get('total', 0)}",
        f"обновлено {summary.get('synced', 0)}",
        f"создано {summary.get('created', 0)}",
        f"пересоздано {summary.get('recreated', 0)}",
        f"без squads {summary.get('empty_squads', 0)}",
        f"ошибок {summary.get('failed', 0)}",
    ]
    if not summary.get("catalog_synced", True):
        parts.append("каталог нод не обновился")
    return "Синхронизация доступов к нодам завершена: " + ", ".join(parts) + "."


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


def portal_login_redirect() -> RedirectResponse:
    return RedirectResponse("/portal/login", status_code=303)


def portal_login_capabilities(settings) -> tuple[bool, str | None, bool]:
    bot_name = (settings.client_bot_name or "").strip().lstrip("@")
    parsed = urlparse(settings.backend_public_url)
    host = (parsed.hostname or "").lower()
    scheme = (parsed.scheme or "").lower()
    local_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    dev_login_enabled = settings.debug or host in local_hosts or scheme != "https" or not host

    if not settings.client_bot_token:
        return (
            False,
            "Не задан CLIENT_BOT_TOKEN. Настройте клиентский бот, чтобы подтверждать вход через Telegram.",
            dev_login_enabled,
        )
    if not bot_name or not TELEGRAM_USERNAME_RE.fullmatch(bot_name):
        return (
            False,
            "Не задан корректный username клиентского Telegram-бота. Укажите CLIENT_BOT_NAME в формате @your_bot.",
            dev_login_enabled,
        )
    return True, None, dev_login_enabled


def portal_bot_login_url(settings, token: str) -> str | None:
    bot_name = (settings.client_bot_name or "").strip().lstrip("@")
    if not bot_name or not TELEGRAM_USERNAME_RE.fullmatch(bot_name):
        return None
    return f"https://t.me/{bot_name}?start=login_{token}"


def portal_login_qr_data_url(payload: str | None) -> str | None:
    if not payload:
        return None
    return f"data:image/png;base64,{base64.b64encode(render_qr_png(payload)).decode('ascii')}"


async def ensure_portal_login_attempt(request: Request, hub):
    requested_token = hub.portal_auth.normalize_token(request.query_params.get("token"))
    if requested_token:
        attempt = await hub.portal_auth.get_login_attempt(requested_token)
        status = hub.portal_auth.login_attempt_status(attempt)
        if status in {"pending", "approved"}:
            request.session[PORTAL_LOGIN_ATTEMPT_SESSION_KEY] = requested_token
            return attempt

    session_token = request.session.get(PORTAL_LOGIN_ATTEMPT_SESSION_KEY)
    if session_token:
        attempt = await hub.portal_auth.get_login_attempt(session_token)
        status = hub.portal_auth.login_attempt_status(attempt)
        if status in {"pending", "approved"}:
            return attempt

    attempt = await hub.portal_auth.create_login_attempt()
    request.session[PORTAL_LOGIN_ATTEMPT_SESSION_KEY] = attempt.token
    return attempt


async def resolve_admin(request: Request, hub):
    admin_id = request.session.get("admin_id")
    if not admin_id:
        return None
    try:
        return await hub.accounts.get_admin(admin_id)
    except Exception:
        request.session.clear()
        return None


async def resolve_portal_user(request: Request, hub):
    session = getattr(request, "session", {})
    portal_user_id = session.get("portal_user_id")
    if not portal_user_id:
        return None
    try:
        return await hub.accounts.get_user(portal_user_id)
    except Exception:
        session.pop("portal_user_id", None)
        return None


async def portal_channel_state(request: Request, user: User) -> bool:
    settings = request.app.state.settings
    if not settings.required_subscription_channel:
        return True
    return await check_channel_membership(
        bot_token=settings.client_bot_token,
        channel=settings.required_subscription_channel,
        user_id=user.telegram_id,
    )


def portal_plan_family(plan) -> str | None:
    if plan is None:
        return None
    raw_code = getattr(plan.code, "value", plan.code)
    if raw_code in {PlanCode.SINGLE_10GBIT.value, PlanCode.SINGLE_10GBIT_WEEKLY.value}:
        return "10gbit"
    if raw_code in {PlanCode.UNLIMITED.value, PlanCode.UNLIMITED_WEEKLY.value}:
        return "unlimited"
    return None


def group_portal_plans(plans) -> list[dict]:
    groups: dict[str, dict] = {}
    order: list[str] = []

    for plan in sorted(plans, key=lambda item: item.sort_order):
        if plan.is_trial:
            continue
        family = portal_plan_family(plan)
        if family is None:
            continue
        if family not in groups:
            weekly_plan_code = (
                PlanCode.SINGLE_10GBIT_WEEKLY if family == "10gbit" else PlanCode.UNLIMITED_WEEKLY
            )
            monthly_plan_code = PlanCode.SINGLE_10GBIT if family == "10gbit" else PlanCode.UNLIMITED
            groups[family] = {
                "family": family,
                "title": "Start" if family == "10gbit" else "Pro",
                "description": plan.description,
                "device_limit": plan.device_limit,
                "whitelist_weekly_gb": WHITELIST_INCLUDED_GB_BY_PLAN[weekly_plan_code],
                "whitelist_monthly_gb": WHITELIST_INCLUDED_GB_BY_PLAN[monthly_plan_code],
                "whitelist_price_per_gb_rub": WHITELIST_GB_PRICE_RUB,
                "periods": [],
            }
            order.append(family)
        groups[family]["periods"].append(
            {
                "label": "На неделю" if plan.period_days <= 7 else "На месяц",
                "caption": "Гибкое продление" if plan.period_days <= 7 else "Основной формат",
                "price_rub": plan.price_rub,
                "price_label": format_rub_amount(plan.price_rub),
                "plan_code": getattr(plan.code, "value", plan.code),
                "period_days": plan.period_days,
            }
        )

    for family in order:
        groups[family]["periods"].sort(key=lambda item: item["period_days"], reverse=True)
    return [groups[family] for family in order]


def document_root() -> Path:
    candidates = [
        Path("/app/document"),
        Path(__file__).resolve().parents[4] / "document",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


def resolve_document_path(kind: str) -> Path:
    root = document_root()
    direct_path = root / DOCUMENT_FILENAMES[kind]
    if direct_path.exists():
        return direct_path

    keywords = DOCUMENT_KEYWORDS.get(kind, ())
    markdown_files = sorted(root.glob("*.md"))
    for path in markdown_files:
        normalized_name = path.stem.casefold()
        if any(keyword.casefold() in normalized_name for keyword in keywords):
            return path
    return direct_path


def load_document_text(kind: str) -> str:
    path = resolve_document_path(kind)
    if not path.exists():
        available = list(document_root().glob("*.md"))
        if available:
            return "Документ пока не опубликован. Проверьте названия файлов в папке document."
        return "Документ пока не опубликован."
    return strip_document_title(path.read_text(encoding="utf-8-sig")).strip()


def strip_document_title(markdown_text: str) -> str:
    lines = markdown_text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if line.lstrip().startswith("# "):
            without_title = "\n".join(lines[index + 1 :]).strip()
            return without_title or markdown_text.strip()
        return markdown_text.strip()
    return markdown_text.strip()


def document_excerpt_text(markdown_text: str) -> str:
    for line in markdown_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped in {"---", "***"}:
            continue
        return re.sub(r"^[#>\-\*\s]+", "", stripped).strip()
    return ""


async def probe_server_latency(server, *, timeout_seconds: float = 2.5) -> dict:
    first_probe = await single_probe_server_latency(server, timeout_seconds=timeout_seconds)
    first_latency = first_probe.get("latency_ms")
    if not first_probe.get("reachable") or first_latency is None or first_latency < LATENCY_RECHECK_THRESHOLD_MS:
        first_probe["rechecked"] = False
        first_probe["attempts"] = 1
        return first_probe

    second_probe = await single_probe_server_latency(server, timeout_seconds=timeout_seconds)
    result = dict(first_probe)
    result["rechecked"] = True
    result["attempts"] = 2
    result["initial_latency_ms"] = first_latency
    if second_probe.get("reachable") and second_probe.get("latency_ms") is not None:
        result["latency_ms"] = second_probe["latency_ms"]
        result["second_latency_ms"] = second_probe["latency_ms"]
        result.pop("error", None)
    else:
        result["second_latency_ms"] = None
        if second_probe.get("error"):
            result["recheck_error"] = second_probe["error"]
    return result


def render_markdown_inline(text: str) -> str:
    rendered = escape(text)
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"`(.+?)`", r"<code>\1</code>", rendered)
    return rendered


def markdown_to_html(markdown_text: str) -> Markup:
    blocks: list[str] = []
    list_items: list[str] = []
    quote_lines: list[str] = []
    paragraph_lines: list[str] = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            blocks.append("<ul>" + "".join(f"<li>{item}</li>" for item in list_items) + "</ul>")
            list_items = []

    def flush_quote() -> None:
        nonlocal quote_lines
        if quote_lines:
            blocks.append("<blockquote>" + "<br>".join(quote_lines) + "</blockquote>")
            quote_lines = []

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        if paragraph_lines:
            blocks.append(f"<p>{'<br>'.join(paragraph_lines)}</p>")
            paragraph_lines = []

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_list()
            flush_quote()
            flush_paragraph()
            continue
        if stripped in {"---", "***"}:
            flush_list()
            flush_quote()
            flush_paragraph()
            blocks.append("<hr>")
            continue
        if stripped.startswith(">"):
            flush_list()
            flush_paragraph()
            quote_lines.append(render_markdown_inline(stripped.lstrip(">").strip()))
            continue
        if stripped.startswith(("- ", "* ")):
            flush_quote()
            flush_paragraph()
            list_items.append(render_markdown_inline(stripped[2:].strip()))
            continue
        heading_level = 0
        while heading_level < len(stripped) and stripped[heading_level] == "#":
            heading_level += 1
        if 1 <= heading_level <= 4 and stripped[heading_level:heading_level + 1] == " ":
            flush_list()
            flush_quote()
            flush_paragraph()
            content = render_markdown_inline(stripped[heading_level + 1 :].strip())
            blocks.append(f"<h{heading_level}>{content}</h{heading_level}>")
            continue
        flush_list()
        flush_quote()
        paragraph_lines.append(render_markdown_inline(stripped))

    flush_list()
    flush_quote()
    flush_paragraph()
    return Markup("\n".join(blocks))


def detect_support_photo_type(content: bytes) -> tuple[str, str] | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def support_upload_root(settings) -> Path:
    return Path(settings.support_upload_dir).resolve()


def support_photo_path(settings, stored_name: str) -> Path | None:
    if not stored_name:
        return None
    root = support_upload_root(settings)
    candidate = (root / stored_name).resolve()
    if candidate.parent != root:
        return None
    return candidate


async def save_support_photo(upload, settings) -> dict[str, object] | None:
    filename = str(getattr(upload, "filename", "") or "").strip()
    read = getattr(upload, "read", None)
    if not filename or read is None:
        return None

    content = await read(settings.support_photo_max_bytes + 1)
    close = getattr(upload, "close", None)
    if close is not None:
        await close()
    if not content:
        return None
    if len(content) > settings.support_photo_max_bytes:
        max_mb = max(settings.support_photo_max_bytes // (1024 * 1024), 1)
        raise ConflictError(f"Фотография слишком большая. Максимальный размер — {max_mb} МБ.")

    detected = detect_support_photo_type(content)
    if detected is None:
        raise ConflictError("Можно прикрепить фотографию в формате JPG, PNG или WebP.")
    mime_type, extension = detected
    root = support_upload_root(settings)
    stored_name = f"{generate_token(24)}{extension}"
    target = support_photo_path(settings, stored_name)
    if target is None:
        raise ConflictError("Не удалось безопасно сохранить фотографию.")
    try:
        root.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    except OSError as exc:
        logger.exception("Failed to save support attachment.")
        raise ServiceError("Не удалось сохранить фотографию. Попробуйте ещё раз.") from exc
    return {
        "attachment_path": stored_name,
        "attachment_mime_type": mime_type,
        "attachment_original_name": Path(filename).name[:255],
        "attachment_size": len(content),
        "absolute_path": target,
    }


def delete_support_photo(settings, attachment: dict[str, object] | None) -> None:
    if not attachment:
        return
    path = attachment.get("absolute_path")
    if isinstance(path, Path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove orphaned support attachment %s.", path, exc_info=True)


def support_attachment_kwargs(attachment: dict[str, object] | None) -> dict[str, object]:
    if not attachment:
        return {}
    return {
        key: attachment[key]
        for key in (
            "attachment_path",
            "attachment_mime_type",
            "attachment_original_name",
            "attachment_size",
        )
    }


async def notify_admins_about_support_request_from_portal(
    settings,
    *,
    admin_telegram_ids: list[int],
    user: User,
    request_id: str,
    topic: str,
    message: str,
    heading: str = "Новый запрос поддержки с сайта",
    photo_path: Path | None = None,
) -> None:
    if not admin_telegram_ids or not settings.admin_bot_token:
        return
    text = "\n".join(
        [
            heading,
            "",
            f"Пользователь: @{user.username}" if user.username else f"Пользователь: Telegram ID {user.telegram_id}",
            f"Telegram ID: {user.telegram_id}",
            f"Тема: {topic}",
            f"Номер: {request_id}",
            "",
            message[:3000],
        ]
    )
    bot = Bot(token=settings.admin_bot_token)
    try:
        for chat_id in admin_telegram_ids:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=support_request_actions(request_id, False).as_markup(),
                )
                if photo_path is not None and photo_path.is_file():
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=FSInputFile(photo_path),
                        caption=f"Фотография к обращению {request_id}",
                        reply_markup=support_request_actions(request_id, False).as_markup(),
                    )
            except Exception:
                logger.warning("Failed to notify admin %s about support request %s.", chat_id, request_id, exc_info=True)
    finally:
        await bot.session.close()


def bot_deep_link(settings, payload: str | None = None) -> str | None:
    bot_name = (settings.client_bot_name or "").strip().lstrip("@")
    if not bot_name or not TELEGRAM_USERNAME_RE.fullmatch(bot_name):
        return None
    if payload:
        return f"https://t.me/{bot_name}?start={payload}"
    return f"https://t.me/{bot_name}"


async def referral_stats(hub, user: User) -> dict[str, object]:
    try:
        referral_count = await hub.session.scalar(
            select(func.count(User.id)).where(User.referred_by_user_id == user.id)
        )
        referral_earned = await hub.session.scalar(
            select(func.coalesce(func.sum(BalanceTransaction.amount_rub), 0)).where(
                BalanceTransaction.user_id == user.id,
                BalanceTransaction.type == BalanceTransactionType.REFERRAL_BONUS,
            )
        )
    except Exception:
        logger.warning("Failed to load referral stats for portal user %s.", user.id, exc_info=True)
        referral_count = 0
        referral_earned = Decimal("0")
    if not isinstance(referral_count, int | float | Decimal):
        referral_count = 0
    try:
        earned_total = Decimal(referral_earned or 0)
    except Exception:
        earned_total = Decimal("0")
    return {
        "count": int(referral_count or 0),
        "earned_total": earned_total,
    }


async def build_portal_context(request: Request, hub, user: User) -> dict:
    bundle = await hub.accounts.get_subscription_bundle(user.id)
    subscription = bundle.get("subscription")
    user_servers = await hub.catalog.get_user_servers(user.id)
    plans = await hub.dashboard.list_plans()
    payments = await hub.topups.list_requests(user_id=user.id)
    channel_ok = await portal_channel_state(request, user)
    show_usage_details = bool(subscription and subscription.plan and is_metered_plan_code(subscription.plan.code))
    trial_available = await hub.accounts.can_offer_trial(user.id)
    server_latency_state, server_latency_checked_at = await load_server_latency_state(hub.session)
    portal_devices, portal_devices_error = await safe_user_hwid_device_views(hub, user.id)
    support_service = getattr(hub, "support", None)
    support_requests = (
        await support_service.list_user_requests(user.id, limit=10)
        if support_service is not None
        else []
    )
    active_support_request = next(
        (item for item in support_requests if item.status != SupportRequestStatus.RESOLVED),
        None,
    )
    referral = await referral_stats(hub, user)
    settings = request.app.state.settings
    client_bot_url = bot_deep_link(settings)
    user_referral_code = getattr(user, "referral_code", None)
    referral_link = bot_deep_link(settings, f"ref_{user_referral_code}") if user_referral_code else None
    topup_amounts = [100, 300, 500, 1000]
    topup_links = [
        {"amount": amount, "url": bot_deep_link(settings, f"pay_{amount}")}
        for amount in topup_amounts
    ]
    billing_service = getattr(hub, "billing", None)
    whitelist_status = (
        await billing_service.get_whitelist_traffic_status(user.id)
        if billing_service is not None
        else None
    )
    whitelist_packages = [
        {
            "code": code,
            "gigabytes": int(item["gigabytes"]),
            "price_rub": Decimal(item["price_rub"]),
            "regular_price_rub": Decimal(item["gigabytes"]) * WHITELIST_GB_PRICE_RUB,
            "request_key": secrets.token_hex(12),
        }
        for code, item in WHITELIST_TRAFFIC_PACKAGES.items()
    ]

    qr_data_uri = None
    info = bundle.get("subscription_info")
    payload = bundle.get("subscription_url") or (info.subscriptionUrl if info else None)
    if payload:
        qr_png = render_qr_png(payload)
        qr_data_uri = f"data:image/png;base64,{base64.b64encode(qr_png).decode('ascii')}"

    current_plan = subscription.plan if subscription else None
    current_plan_code = getattr(current_plan.code, "value", current_plan.code) if current_plan else None
    request_session = getattr(request, "session", None)
    portal_link_reissued = bool(request_session.pop("portal_link_reissued", False)) if request_session is not None else False

    return {
        "title": "Личный кабинет",
        "portal_user": user,
        "portal_subscription": subscription,
        "portal_current_plan_code": current_plan_code,
        "portal_current_plan_family": portal_plan_family(current_plan),
        "portal_bundle": bundle,
        "portal_servers": user_servers,
        "portal_plans": plans,
        "portal_plan_groups": group_portal_plans(plans),
        "portal_payments": payments,
        "portal_server_latency_state": server_latency_state,
        "portal_server_latency_checked_at": server_latency_checked_at,
        "portal_qr_data_uri": qr_data_uri,
        "portal_channel_ok": channel_ok,
        "portal_show_usage_details": show_usage_details,
        "portal_trial_available": trial_available,
        "portal_devices": portal_devices,
        "portal_devices_error": portal_devices_error,
        "portal_subscription_payload": payload,
        "portal_link_reissued": portal_link_reissued,
        "portal_support_requests": support_requests,
        "portal_active_support_request": active_support_request,
        "portal_referral_count": referral["count"],
        "portal_referral_earned_total": referral["earned_total"],
        "portal_referral_link": referral_link,
        "portal_topup_links": topup_links,
        "portal_min_topup_amount_rub": MIN_TOPUP_AMOUNT_RUB,
        "portal_whitelist_status": whitelist_status,
        "portal_whitelist_packages": whitelist_packages,
        "portal_url": f"{settings.backend_public_url.rstrip('/')}/portal",
        "client_bot_url": client_bot_url,
        "support_url": "https://t.me/altlink_support",
        "connection_help_url": f"{settings.backend_public_url.rstrip('/')}/help/connect",
        "agreement_page_url": f"{settings.backend_public_url.rstrip('/')}/legal/agreement",
        "privacy_page_url": f"{settings.backend_public_url.rstrip('/')}/legal/privacy",
        "latency_api_url": "/api/latency",
        "latency_target_label": latency_target_label(),
        "latency_disclaimer": latency_disclaimer_text(),
        "required_channel_url": settings.required_subscription_channel_url,
        "whitelist_price_per_gb": WHITELIST_GB_PRICE_RUB,
        "whitelist_cost_rub": bytes_to_gb_cost(
            subscription.whitelist_traffic_used_bytes if subscription else 0,
            WHITELIST_GB_PRICE_RUB,
        ),
        "telegram_login_bot": settings.client_bot_name.lstrip("@"),
    }


async def safe_user_hwid_device_views(hub, user_id: str) -> tuple[list[dict[str, object]], str | None]:
    try:
        devices = await hub.accounts.list_user_hwid_devices(user_id)
    except ServiceError as exc:
        return [], str(exc)
    except Exception:
        logger.warning("Failed to load HWID devices for user %s.", user_id, exc_info=True)
        return [], "Не удалось загрузить устройства из панели. Попробуйте обновить страницу чуть позже."
    return [hwid_device_view(item) for item in devices], None


@router.get("/")
async def landing_page(request: Request):
    async with request.app.state.container.hub() as hub:
        plans = await hub.dashboard.list_plans()
        servers = await hub.catalog.list_servers()
        server_latency_state, server_latency_checked_at = await load_server_latency_state(hub.session)
        portal_user = await resolve_portal_user(request, hub)
    settings = request.app.state.settings
    portal_authenticated = portal_user is not None
    portal_login_url = "/portal" if portal_authenticated else "/portal/login?autostart=1"
    paid_device_limits = [plan.device_limit for plan in plans if not plan.is_trial and plan.device_limit]
    landing_max_device_limit = max(paid_device_limits) if paid_device_limits else None
    paid_weekly_prices = [
        Decimal(plan.price_rub)
        for plan in plans
        if not plan.is_trial and plan.period_days <= 7
    ]
    landing_min_weekly_price_label = (
        format_rub_amount(min(paid_weekly_prices)) if paid_weekly_prices else None
    )
    landing_latency_items = build_landing_latency_items(servers, server_latency_state)
    landing_location_items = build_landing_location_items(landing_latency_items)
    landing_latency_values = [
        item["latency_ms"]
        for item in landing_latency_items
        if isinstance(item.get("latency_ms"), int | float)
    ]
    landing_latency_best_label = f"{round(min(landing_latency_values))} мс" if landing_latency_values else "—"
    landing_latency_initial_hint = "Проверьте задержку до серверов ALTLINK прямо из браузера. Чем ниже пинг, тем быстрее отклик."
    support_username = settings.support_username or "@altlink_support"
    support_url = "https://t.me/altlink_support"
    return render(
        request,
        "landing.html",
        title="ALTLINK VPN — быстрый и конфиденциальный доступ",
        portal_plan_groups=group_portal_plans(plans),
        landing_max_device_limit=landing_max_device_limit,
        landing_min_weekly_price_label=landing_min_weekly_price_label,
        portal_login_url=portal_login_url,
        landing_portal_authenticated=portal_authenticated,
        landing_account_button_label="Личный кабинет" if portal_authenticated else "Войти",
        connection_help_url=f"{settings.backend_public_url.rstrip('/')}/help/connect",
        agreement_page_url=f"{settings.backend_public_url.rstrip('/')}/legal/agreement",
        privacy_page_url=f"{settings.backend_public_url.rstrip('/')}/legal/privacy",
        client_bot_url=f"https://t.me/{settings.client_bot_name.lstrip('@')}" if settings.client_bot_name else None,
        support_username=support_username,
        support_url=support_url,
        latency_api_url="/api/latency",
        latency_target_label=latency_target_label(),
        latency_disclaimer=latency_disclaimer_text(),
        landing_latency_items=landing_latency_items,
        landing_location_items=landing_location_items,
        landing_latency_checked_at=server_latency_checked_at,
        landing_latency_best_label=landing_latency_best_label,
        landing_latency_initial_hint=landing_latency_initial_hint,
    )


@router.get("/api/latency")
async def latency_probe(request: Request) -> JSONResponse:
    query_params = getattr(request, "query_params", None)
    raw_server_ids = query_params.get("server_ids", "") if query_params is not None else ""
    requested_server_ids = {item.strip() for item in raw_server_ids.split(",") if item.strip()}
    include_local = (
        str(query_params.get("include_local", "0")).strip().lower() in {"1", "true", "yes"}
        if query_params is not None
        else False
    )

    async with request.app.state.container.hub() as hub:
        servers = await hub.catalog.list_servers()
    settings = request.app.state.settings
    targets = [
        server
        for server in servers
        if getattr(server, "is_available", False)
        and (not requested_server_ids or getattr(server, "id", None) in requested_server_ids)
        and (include_local or is_foreign_latency_target(server))
    ]
    probes = [
        {
            "server_id": getattr(server, "id", None),
            "name": getattr(server, "name", None),
            "address": getattr(server, "address", None),
            "country_code": (getattr(server, "country_code", "") or "").upper(),
            "country_name": country_name(getattr(server, "country_code", None)),
            "country_flag": country_flag(getattr(server, "country_code", None)),
            "is_connected": bool(getattr(server, "is_connected", False)),
            "probe_scheme": settings.latency_probe_scheme,
            "probe_port": settings.latency_probe_port,
            "probe_path": settings.latency_probe_path,
            "probe_url": browser_probe_url(
                server,
                scheme=settings.latency_probe_scheme,
                port=settings.latency_probe_port,
                path=settings.latency_probe_path,
            ),
        }
        for server in targets
    ]
    probes = [probe for probe in probes if probe.get("server_id") and probe.get("probe_url")]
    probes = sorted(
        probes,
        key=lambda item: (
            item.get("country_code", ""),
            item.get("name", ""),
        ),
    )
    return JSONResponse(
        {
            "ok": True,
            "target": "node_probe_endpoints",
            "source": "browser",
            "measurement_mode": "browser_rtt",
            "count": len(probes),
            "timeout_ms": settings.browser_latency_timeout_ms,
            "recheck_threshold_ms": LATENCY_RECHECK_THRESHOLD_MS,
            "disclaimer": latency_disclaimer_text(),
            "probes": probes,
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@router.get("/help/connect")
async def connect_help_page(request: Request):
    return render(
        request,
        "portal_help.html",
        title="Быстрое подключение",
        support_username=request.app.state.settings.support_username,
    )


@router.get("/legal/agreement")
async def agreement_page(request: Request):
    document_text = load_document_text("agreement")
    return render(
        request,
        "legal_agreement.html",
        title="Пользовательское соглашение",
        document_html=markdown_to_html(document_text),
        document_excerpt=document_excerpt_text(document_text),
    )


@router.get("/legal/privacy")
async def privacy_page(request: Request):
    document_text = load_document_text("privacy")
    return render(
        request,
        "legal_privacy.html",
        title="Политика конфиденциальности",
        document_html=markdown_to_html(document_text),
        document_excerpt=document_excerpt_text(document_text),
    )


@router.get("/admin/login")
async def login_page(request: Request, error: str | None = None):
    return render(request, "login.html", title="Вход", error=error)


@router.post("/admin/login")
async def login_submit(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        try:
            admin = await hub.accounts.authenticate_admin(form.get("username", ""), form.get("password", ""))
        except Exception:
            return render(request, "login.html", title="Вход", error="Неверный логин или пароль.")
        request.session["admin_id"] = admin.id
        return RedirectResponse("/admin/dashboard", status_code=303)


@router.post("/admin/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


@router.get("/admin/dashboard")
async def dashboard(request: Request, period: str = "2w", refresh: bool = False):
    container = request.app.state.container
    async with container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        summary = await hub.dashboard.summary()
        return render(
            request,
            "dashboard.html",
            title="Dashboard",
            admin=admin,
            summary=summary,
            active_nav="dashboard",
        )


@router.get("/admin/analytics")
async def analytics(request: Request, period: str = "2w"):
    container = request.app.state.container
    selected_server_ids = request.query_params.getlist("server_id")
    async with container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()

    async def load_business_analytics() -> dict:
        async with container.hub() as hub:
            return await hub.dashboard.overview(period=period)

    async def load_server_analytics() -> tuple[dict, str | None]:
        async with container.hub() as hub:
            try:
                result = await hub.dashboard.server_analytics(
                    period=period,
                    selected_server_ids=selected_server_ids,
                )
                return result, None
            except SQLAlchemyError:
                logger.exception("Failed to load server metric history; using current server snapshot.")
                await hub.session.rollback()
                result = await hub.dashboard.current_server_analytics(
                    period=period,
                    selected_server_ids=selected_server_ids,
                )
                return result, (
                    "История серверов пока недоступна. Показан текущий срез; "
                    "проверьте применение миграций базы данных."
                )

    overview, (server_analytics, analytics_warning) = await asyncio.gather(
        load_business_analytics(),
        load_server_analytics(),
    )
    charts = {
        "business": overview["charts"],
        "servers": server_analytics["charts"],
    }
    return render(
        request,
        "analytics.html",
        title="Аналитика",
        admin=admin,
        overview=overview,
        server_analytics=server_analytics,
        analytics_warning=analytics_warning,
        charts=charts,
        charts_json=json.dumps(charts, ensure_ascii=False),
        selected_period=overview["period"],
        active_nav="analytics",
    )


@router.get("/admin/users")
async def users_page(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    plan: str | None = None,
    balance_min: str | None = None,
    balance_max: str | None = None,
    last_seen_from: str | None = None,
    last_seen_to: str | None = None,
    traffic_min: str | None = None,
    traffic_max: str | None = None,
    whitelist_traffic_min: str | None = None,
    whitelist_traffic_max: str | None = None,
    node_id: str | None = None,
    node_traffic_min: str | None = None,
    node_traffic_max: str | None = None,
    next_billing_from: str | None = None,
    next_billing_to: str | None = None,
    registered_from: str | None = None,
    registered_to: str | None = None,
    devices_min: str | None = None,
    devices_max: str | None = None,
    sort: str = "created_at",
    direction: str = "desc",
    limit: int = DEFAULT_USER_LIST_PAGE_SIZE,
):
    filter_values = {
        "search": search or "",
        "status": status or "",
        "plan": plan or "",
        "balance_min": balance_min or "",
        "balance_max": balance_max or "",
        "last_seen_from": last_seen_from or "",
        "last_seen_to": last_seen_to or "",
        "traffic_min": traffic_min or "",
        "traffic_max": traffic_max or "",
        "whitelist_traffic_min": whitelist_traffic_min or "",
        "whitelist_traffic_max": whitelist_traffic_max or "",
        "node_id": node_id or "",
        "node_traffic_min": node_traffic_min or "",
        "node_traffic_max": node_traffic_max or "",
        "next_billing_from": next_billing_from or "",
        "next_billing_to": next_billing_to or "",
        "registered_from": registered_from or "",
        "registered_to": registered_to or "",
        "devices_min": devices_min or "",
        "devices_max": devices_max or "",
        "sort": sort or "created_at",
        "direction": direction if direction in {"asc", "desc"} else "desc",
        "limit": str(normalize_user_list_limit(limit)),
    }
    filters = UserListFilters(
        search=search,
        status=status,
        plan=plan,
        balance_min=parse_decimal_query(balance_min),
        balance_max=parse_decimal_query(balance_max),
        last_seen_from=parse_date_query(last_seen_from),
        last_seen_to=parse_date_query(last_seen_to, end_of_day=True),
        traffic_min_bytes=parse_gb_to_bytes(traffic_min),
        traffic_max_bytes=parse_gb_to_bytes(traffic_max),
        whitelist_traffic_min_bytes=parse_gb_to_bytes(whitelist_traffic_min),
        whitelist_traffic_max_bytes=parse_gb_to_bytes(whitelist_traffic_max),
        node_id=node_id,
        node_traffic_min_bytes=parse_gb_to_bytes(node_traffic_min),
        node_traffic_max_bytes=parse_gb_to_bytes(node_traffic_max),
        next_billing_from=parse_date_query(next_billing_from),
        next_billing_to=parse_date_query(next_billing_to, end_of_day=True),
        registered_from=parse_date_query(registered_from),
        registered_to=parse_date_query(registered_to, end_of_day=True),
        device_count_min=parse_int_query(devices_min),
        device_count_max=parse_int_query(devices_max),
        sort=filter_values["sort"],
        direction=filter_values["direction"],
        limit=normalize_user_list_limit(limit),
    )
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        page = await hub.accounts.list_users_for_admin(filters)
        plans = await hub.dashboard.list_plans()
        servers = await hub.catalog.list_servers()
        return render(
            request,
            "users.html",
            title="Пользователи",
            admin=admin,
            users=page.users,
            user_page=page,
            filters=filter_values,
            search=search or "",
            status_options=[{"value": item.value, "label": user_status_label(item)} for item in UserStatus],
            plan_filter_options=[
                {"value": "start", "label": "Start"},
                {"value": "pro", "label": "Pro"},
                {"value": "trial", "label": "Тест"},
                {"value": "paid", "label": "Любой платный"},
                {"value": "none", "label": "Без тарифа"},
            ],
            plans=plans,
            servers=servers,
            limit_options=USER_LIST_PAGE_SIZE_OPTIONS,
            sort_options=[
                {"value": "created_at", "label": "Дата регистрации"},
                {"value": "username", "label": "Пользователь"},
                {"value": "status", "label": "Статус"},
                {"value": "plan", "label": "Тариф"},
                {"value": "balance", "label": "Баланс"},
                {"value": "last_seen", "label": "Последняя активность"},
                {"value": "traffic", "label": "Трафик"},
                {"value": "whitelist_traffic", "label": "Whitelist-трафик"},
                {"value": "node_traffic", "label": "Трафик на ноде"},
                {"value": "next_billing", "label": "Следующее списание"},
                {"value": "devices", "label": "Устройства"},
            ],
            csrf_token=get_csrf_token(request),
            active_nav="users",
        )


@router.get("/admin/users/{user_id}")
async def user_detail(request: Request, user_id: str):
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        card = await hub.accounts.user_card(user_id)
        bundle = await hub.accounts.get_subscription_bundle(user_id)
        user_servers = await hub.catalog.get_user_servers(user_id)
        available_start_servers = await hub.catalog.list_available_start_servers()
        plans = await hub.dashboard.list_plans()
        devices, devices_error = await safe_user_hwid_device_views(hub, user_id)
        has_paid_history = await hub.accounts.has_paid_subscription_history(user_id)
        promo_setting = await hub.session.scalar(
            select(SystemSetting).where(SystemSetting.key == PROMO_CAMPAIGN_SETTINGS_KEY)
        )
        promo_settings = normalize_promo_campaign_settings(promo_setting.value if promo_setting is not None else None)
        whitelist_status = await hub.billing.get_whitelist_traffic_status(user_id)
        whitelist_purchases = await hub.billing.list_whitelist_package_purchases(user_id, limit=20)
        return render(
            request,
            "user_detail.html",
            title="Карточка пользователя",
            admin=admin,
            card=card,
            bundle=bundle,
            user_servers=user_servers,
            available_start_servers=available_start_servers,
            assigned_start_server_usable=hub.catalog.is_server_usable(card["user"].assigned_server),
            is_start_subscription=bool(
                card["subscription"]
                and card["subscription"].plan
                and is_metered_plan_code(card["subscription"].plan.code)
            ),
            plans=plans,
            devices=devices,
            devices_error=devices_error,
            promo_template_options=promo_template_options(),
            promo_default_discount=(
                promo_settings["lapsed_user_discount_percent"]
                if has_paid_history
                else promo_settings["new_user_discount_percent"]
            ),
            traffic_limit_gb=(
                Decimal(card["user"].traffic_limit_bytes_override) / Decimal(BYTES_PER_GIB)
                if card["user"].traffic_limit_bytes_override is not None
                else None
            ),
            traffic_limit_strategy_options=[
                {"value": item.value, "label": TRAFFIC_LIMIT_STRATEGY_LABELS[item]}
                for item in TrafficLimitStrategy
            ],
            whitelist_cost_rub=bytes_to_gb_cost(
                card["subscription"].whitelist_traffic_used_bytes if card["subscription"] else 0,
                WHITELIST_GB_PRICE_RUB,
            ),
            whitelist_status=whitelist_status,
            whitelist_purchases=whitelist_purchases,
            whitelist_grant_request_key=secrets.token_hex(16),
            active_nav="users",
        )


@router.post("/admin/users/{user_id}/start-server")
async def user_start_server(request: Request, user_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    server_id = str(form.get("server_id") or "").strip()
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            server = await hub.catalog.reassign_start_server(
                user_id,
                server_id,
                admin_id=admin.id,
            )
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
        else:
            set_flash(request, f"Пользователю назначен Start-сервер «{server.name}».")
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/admin/users/{user_id}/traffic-limit")
async def user_traffic_limit(request: Request, user_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    raw_limit = str(form.get("limit_gb") or "").strip()
    try:
        limit_gb = Decimal(raw_limit.replace(",", ".")) if raw_limit else None
    except Exception:
        set_flash(request, "Укажите корректное количество гигабайт.", "danger")
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            await hub.billing.set_user_traffic_limit(
                user_id,
                limit_gb=limit_gb,
                strategy=str(form.get("strategy") or TrafficLimitStrategy.NO_RESET.value),
                admin_id=admin.id,
            )
        except (ConflictError, NotFoundError, ServiceError, ValueError) as exc:
            set_flash(request, str(exc), "danger")
        else:
            if limit_gb is None or limit_gb == 0:
                set_flash(request, "Персональный лимит снят. Будет использоваться лимит тарифа.")
            else:
                set_flash(request, "Персональный лимит сохранён и синхронизирован с Remnawave.")
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/admin/users/{user_id}/promo-message")
async def user_send_promo_message(request: Request, user_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    try:
        template_id = int(form.get("template_id") or 1)
    except (TypeError, ValueError):
        template_id = 1
    if template_id not in PROMO_MESSAGE_TEMPLATES:
        template_id = 1

    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        user = await hub.accounts.get_user(user_id)
        promo_setting = await hub.session.scalar(
            select(SystemSetting).where(SystemSetting.key == PROMO_CAMPAIGN_SETTINGS_KEY)
        )
        promo_settings = normalize_promo_campaign_settings(promo_setting.value if promo_setting is not None else None)
        has_paid_history = await hub.accounts.has_paid_subscription_history(user_id)
        default_discount = (
            promo_settings["lapsed_user_discount_percent"]
            if has_paid_history
            else promo_settings["new_user_discount_percent"]
        )
        try:
            discount_percent = int(form.get("discount_percent") or default_discount)
        except (TypeError, ValueError):
            discount_percent = int(default_discount)
        discount_percent = min(max(discount_percent, 1), 100)
        template_kind = promo_template_kind(template_id)

        if template_kind == "trial":
            message = render_promo_campaign_message(
                template_id,
                trial_days=int(request.app.state.settings.trial_duration_days or 2),
            )
            payload = {
                "campaign": "manual_promo",
                "campaign_kind": "manual_return_trial",
                "template_id": template_id,
                "trial_days": int(request.app.state.settings.trial_duration_days or 2),
                "cta": "return_trial",
                "parse_mode": "HTML",
                "admin_id": admin.id,
            }
            await hub.notifications.queue(
                user_id=user.id,
                notification_type=NotificationType.BROADCAST,
                message=message,
                payload=payload,
            )
        else:
            promo = await hub.promos.get_or_create_personal_discount_code(
                user.id,
                discount_percent=Decimal(discount_percent),
                campaign_key=f"manual:{template_id}:{datetime.now(UTC):%Y%m%d%H%M%S}",
            )
            message = render_promo_campaign_message(
                template_id,
                promo_code=promo.code,
                discount_percent=discount_percent,
            )
            payload = {
                "promo_code": promo.code,
                "discount_percent": discount_percent,
                "campaign": "manual_promo",
                "campaign_kind": "manual_discount",
                "template_id": template_id,
                "cta": "inactive_promo",
                "parse_mode": "HTML",
                "admin_id": admin.id,
            }
            await hub.notifications.queue(
                user_id=user.id,
                notification_type=NotificationType.PROMO_CODE,
                message=message,
                payload=payload,
            )
        set_flash(request, "Промо-сообщение поставлено в очередь отправки.", "success")
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/admin/users/sync-access")
async def users_sync_access(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            summary = await hub.billing.sync_users_with_available_nodes()
        except (ConflictError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
        except Exception:
            logger.warning("Manual user node access sync failed from web admin.", exc_info=True)
            set_flash(request, "Не удалось синхронизировать доступы к нодам. Проверьте раздел событий и логи.", "danger")
        else:
            level = "warning" if int(summary.get("failed", 0) or 0) else "success"
            set_flash(request, format_user_node_access_sync_flash(summary), level)
        return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/balance")
async def user_balance(request: Request, user_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    amount = Decimal(str(form.get("amount", "0")))
    description = form.get("description", "Ручная корректировка баланса")
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        await hub.accounts.adjust_balance(
            user_id=user_id,
            amount_rub=amount,
            transaction_type=BalanceTransactionType.MANUAL_ADJUSTMENT,
            description=description,
            admin_id=admin.id,
        )
        await hub.catalog.rebuild_user_access_matrix()
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/admin/users/{user_id}/whitelist-traffic")
async def user_whitelist_traffic_grant(request: Request, user_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    try:
        gigabytes = Decimal(str(form.get("gigabytes") or "0").replace(",", "."))
    except Exception:
        set_flash(request, "Укажите корректное количество гигабайт.", "danger")
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)
    reason = str(form.get("reason") or "").strip()
    request_key = str(form.get("request_key") or "")[:64] or secrets.token_hex(16)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            purchase = await hub.billing.grant_whitelist_traffic(
                user_id,
                gigabytes=gigabytes,
                request_key=request_key,
                admin_id=admin.id,
                reason=reason,
            )
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
        else:
            set_flash(
                request,
                f"Начислено {purchase.traffic_bytes / 1024**3:g} ГБ дополнительного БС-трафика.",
            )
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/admin/users/{user_id}/trial")
async def user_trial(request: Request, user_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            await hub.billing.activate_trial(user_id)
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/admin/users/{user_id}/plan")
async def user_plan(request: Request, user_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    plan_code = parse_paid_plan_code(form.get("plan_code"))
    if plan_code is None:
        set_flash(request, "Тариф больше не поддерживается. Обновите страницу и выберите актуальный вариант.", "danger")
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)
    charge_user = form.get("charge_user") == "1"
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            await hub.billing.activate_paid_plan(user_id, plan_code, charge_user=charge_user, admin_id=admin.id)
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/admin/users/{user_id}/activate")
async def user_activate(request: Request, user_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        await hub.billing.reactivate_user(user_id)
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/admin/users/{user_id}/deactivate")
async def user_deactivate(request: Request, user_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        await hub.billing.deactivate_user(user_id)
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.get("/admin/servers")
async def servers_page(request: Request):
    container = request.app.state.container
    async with container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()

    async with container.hub() as sync_hub:
        await sync_server_catalog_if_possible(sync_hub)

    async with container.hub() as hub:
        servers = await hub.catalog.list_servers()
        server_latency_state, server_latency_checked_at = await load_server_latency_state(hub.session)
        return render(
            request,
            "servers.html",
            title="Серверы",
            admin=admin,
            servers=servers,
            server_latency_state=server_latency_state,
            server_latency_checked_at=server_latency_checked_at,
            server_types=list(ServerType),
            active_nav="servers",
        )


@router.post("/admin/servers/sync")
async def servers_sync(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            await hub.catalog.sync_servers()
        except Exception:
            logger.warning("Manual server sync failed from web admin.", exc_info=True)
            await hub.session.rollback()
        return RedirectResponse("/admin/servers", status_code=303)


@router.post("/admin/servers/{server_id}/toggle")
async def server_toggle(request: Request, server_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    available = form.get("is_available") == "1"
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            await hub.catalog.set_server_availability(server_id, available)
        except NotFoundError:
            return RedirectResponse("/admin/servers", status_code=303)
        return RedirectResponse("/admin/servers", status_code=303)


@router.post("/admin/servers/{server_id}/capacity")
async def server_capacity(request: Request, server_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    max_clients = int(form.get("max_clients", "0"))
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            await hub.catalog.set_server_capacity(server_id, max_clients)
        except NotFoundError:
            return RedirectResponse("/admin/servers", status_code=303)
        return RedirectResponse("/admin/servers", status_code=303)


@router.post("/admin/servers/{server_id}/type")
async def server_type(request: Request, server_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    raw_type = form.get("server_type", ServerType.REGULAR.value)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            await hub.catalog.set_server_type(server_id, ServerType(raw_type))
        except NotFoundError:
            return RedirectResponse("/admin/servers", status_code=303)
        return RedirectResponse("/admin/servers", status_code=303)


@router.post("/admin/servers/{server_id}/force-delete")
async def server_force_delete(request: Request, server_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            await hub.catalog.force_delete_server(server_id)
        except NotFoundError:
            return RedirectResponse("/admin/servers", status_code=303)
        return RedirectResponse("/admin/servers", status_code=303)


@router.get("/admin/topups")
async def topups_page(request: Request, status_filter: str | None = None):
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        status_value = TopupStatus(status_filter) if status_filter else None
        topups = await hub.topups.list_requests(status=status_value)
        return render(
            request,
            "topups.html",
            title="Платежи",
            admin=admin,
            topups=topups,
            status_filter=status_filter or "",
            active_nav="topups",
        )


@router.get("/admin/plans")
async def plans_page(request: Request):
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        plans = await hub.dashboard.list_plans()
        return render(
            request,
            "plans.html",
            title="Тарифы",
            admin=admin,
            plans=plans,
            active_nav="plans",
        )


@router.get("/admin/transactions")
async def transactions_page(
    request: Request,
    search: str | None = None,
    type: str | None = None,
    amount_min: str | None = None,
    amount_max: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = 100,
):
    try:
        transaction_type = BalanceTransactionType(type) if type else None
    except ValueError:
        transaction_type = None
    normalized_limit = limit if limit in {25, 50, 100, 250, 500} else 100
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        transactions = await hub.dashboard.list_transactions(
            search=search,
            transaction_type=transaction_type,
            amount_min=parse_decimal_query(amount_min),
            amount_max=parse_decimal_query(amount_max),
            created_from=parse_date_query(created_from),
            created_to=parse_date_query(created_to, end_of_day=True),
            limit=normalized_limit,
        )
        return render(
            request,
            "transactions.html",
            title="Баланс и транзакции",
            admin=admin,
            transactions=transactions,
            filters={
                "search": search or "",
                "type": type or "",
                "amount_min": amount_min or "",
                "amount_max": amount_max or "",
                "created_from": created_from or "",
                "created_to": created_to or "",
                "limit": normalized_limit,
            },
            transaction_type_options=[
                {"value": item.value, "label": balance_transaction_type_label(item)}
                for item in BalanceTransactionType
            ],
            active_nav="transactions",
        )


@router.get("/admin/traffic")
async def traffic_page(request: Request):
    container = request.app.state.container
    async with container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
    await sync_dashboard_traffic_if_possible(container)
    async with container.hub() as hub:
        subscriptions = await hub.dashboard.list_traffic_rows()
        return render(
            request,
            "traffic.html",
            title="Трафик и начисления",
            admin=admin,
            subscriptions=subscriptions,
            whitelist_price_per_gb=WHITELIST_GB_PRICE_RUB,
            active_nav="traffic",
        )


@router.get("/admin/online")
async def online_page(request: Request, refresh: int = 0):
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        if refresh:
            await hub.online.refresh_online_cache(detailed=True)
        records = await hub.online.list_online(only_online=False)
        return render(
            request,
            "online.html",
            title="Онлайн клиенты",
            admin=admin,
            records=records,
            active_nav="online",
        )


@router.get("/admin/promos")
async def promos_page(request: Request):
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        setting = await hub.session.scalar(
            select(SystemSetting).where(SystemSetting.key == PROMO_CAMPAIGN_SETTINGS_KEY)
        )
        promo_settings = normalize_promo_campaign_settings(setting.value if setting is not None else None)
        stats = await promo_admin_stats(hub.session)
        manual_codes = await hub.promos.list_codes(limit=20)
        return render(
            request,
            "promos.html",
            title="Промо",
            admin=admin,
            promo_settings=promo_settings,
            promo_stats=stats,
            promo_charts_json=json.dumps(stats["charts"], ensure_ascii=False),
            manual_codes=manual_codes,
            promo_template_options=promo_template_options(),
            promo_template_previews=[
                {
                    "id": item["id"],
                    "label": item["label"],
                    "kind": item["kind"],
                    "preview": promo_template_preview(
                        int(item["id"]),
                        discount_percent=int(promo_settings["lapsed_user_discount_percent"]),
                        trial_days=int(request.app.state.settings.trial_duration_days or 2),
                    ),
                }
                for item in promo_template_options()
            ],
            active_nav="promos",
        )


@router.post("/admin/promos/settings")
async def promos_settings_save(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    value = normalize_promo_campaign_settings(
        {
            "new_user_discount_percent": form.get("new_user_discount_percent"),
            "lapsed_user_discount_percent": form.get("lapsed_user_discount_percent"),
            "inactive_first_delay_days": form.get("inactive_first_delay_days"),
            "lapsed_first_delay_days": form.get("lapsed_first_delay_days"),
            "deep_winback_delay_days": form.get("deep_winback_delay_days"),
            "return_trial_enabled": form.get("return_trial_enabled") == "1",
            "return_trial_cooldown_days": form.get("return_trial_cooldown_days"),
        }
    )
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        setting = await hub.session.scalar(
            select(SystemSetting).where(SystemSetting.key == PROMO_CAMPAIGN_SETTINGS_KEY)
        )
        if setting is None:
            setting = SystemSetting(
                key=PROMO_CAMPAIGN_SETTINGS_KEY,
                description="Настройки автоматических промокодов и повторного тестового периода.",
            )
            hub.session.add(setting)
        setting.value = value
        setting.updated_by_admin_id = admin.id
        set_flash(request, "Настройки промо сохранены.", "success")
        return RedirectResponse("/admin/promos", status_code=303)


@router.post("/admin/promos/create")
async def promos_create(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            try:
                reward_kind = PromoRewardKind(str(form.get("reward_kind") or ""))
            except ValueError as exc:
                raise ConflictError("Выберите корректный тип промокода.") from exc
            try:
                reward_value = Decimal(str(form.get("reward_value") or "").replace(",", "."))
            except InvalidOperation as exc:
                raise ConflictError("Укажите корректный размер награды.") from exc

            usage_limit_raw = str(form.get("usage_limit") or "").strip()
            try:
                usage_limit = int(usage_limit_raw) if usage_limit_raw else None
            except ValueError as exc:
                raise ConflictError("Лимит использований должен быть целым числом.") from exc

            try:
                expires_at = parse_msk_datetime_input(str(form.get("expires_at") or ""))
            except ConflictError as exc:
                raise ConflictError("Некорректный срок действия промокода.") from exc

            promo = await hub.promos.create_code(
                code=str(form.get("code") or ""),
                name=str(form.get("name") or ""),
                reward_kind=reward_kind,
                reward_value=reward_value,
                usage_limit=usage_limit,
                expires_at=expires_at,
                new_users_only=form.get("new_users_only") == "1",
                admin_id=admin.id,
            )
        except (ConflictError, InvalidOperation) as exc:
            set_flash(request, str(exc), "danger")
        else:
            set_flash(request, f"Промокод {promo.code} создан.", "success")
        return RedirectResponse("/admin/promos", status_code=303)


@router.get("/admin/settings")
async def settings_page(request: Request):
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        settings_list = await hub.dashboard.list_settings()
        settings_by_key = {item.key: item for item in settings_list}
        whitelist_server_setting = settings_by_key.get(WHITELIST_SERVER_DOMAIN_SETTING_KEY) or settings_by_key.get(
            LEGACY_WHITELIST_LATENCY_TARGET_SETTING_KEY
        )
        return render(
            request,
            "settings.html",
            title="Настройки",
            admin=admin,
            settings_list=settings_list,
            whitelist_server_domain_key=WHITELIST_SERVER_DOMAIN_SETTING_KEY,
            whitelist_server_domain_value=normalize_latency_target_domain(
                getattr(whitelist_server_setting, "value", None)
            )
            or "",
            active_nav="settings",
        )


@router.post("/admin/settings")
async def settings_save(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    key = form.get("key", "").strip()
    value_raw = form.get("value", "").strip()
    description = form.get("description") or None
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        if key == WHITELIST_SERVER_DOMAIN_SETTING_KEY:
            normalized_domain = normalize_latency_target_domain(value_raw)
            if value_raw and normalized_domain is None:
                set_flash(
                    request,
                    "Укажите корректный домен или URL сервера белых списков.",
                    "danger",
                )
                return RedirectResponse("/admin/settings", status_code=303)
            parsed_value = normalized_domain
            if not description:
                description = "Домен сервера для latency-проверки серверов белых списков."
        else:
            try:
                parsed_value = json.loads(value_raw)
            except json.JSONDecodeError:
                parsed_value = value_raw
        setting = await hub.session.scalar(select(SystemSetting).where(SystemSetting.key == key))
        if setting is None:
            setting = SystemSetting(key=key)
            hub.session.add(setting)
        setting.value = parsed_value
        setting.description = description
        setting.updated_by_admin_id = admin.id
        set_flash(request, "Настройка сохранена.", "success")
        return RedirectResponse("/admin/settings", status_code=303)


def external_api_base_url(request: Request) -> str:
    configured = (request.app.state.settings.backend_public_url or "").strip().rstrip("/")
    return configured or str(request.base_url).rstrip("/")


async def render_api_clients_page(
    request: Request,
    *,
    admin,
    hub,
    issued_key: str | None = None,
    issued_client=None,
    error: str | None = None,
):
    clients = await hub.external_api.list_clients()
    return render(
        request,
        "api_clients.html",
        title="Внешний API",
        admin=admin,
        clients=clients,
        scope_definitions=EXTERNAL_API_SCOPE_DEFINITIONS,
        recommended_scopes=EXTERNAL_API_RECOMMENDED_SCOPES,
        issued_key=issued_key,
        issued_client=issued_client,
        error=error,
        api_base_url=f"{external_api_base_url(request)}/api/external/v1",
        active_nav="api",
    )


@router.get("/admin/api-clients")
async def api_clients_page(request: Request):
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        return await render_api_clients_page(request, admin=admin, hub=hub)


@router.post("/admin/api-clients")
async def api_clients_create(request: Request):
    form = await request.form()
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            issued = await hub.external_api.create_client(
                name=str(form.get("name") or ""),
                description=str(form.get("description") or ""),
                scopes=list(form.getlist("scopes")),
                expires_at=parse_msk_datetime_input(str(form.get("expires_at") or "")),
                admin_id=admin.id,
            )
        except ConflictError as exc:
            return await render_api_clients_page(
                request,
                admin=admin,
                hub=hub,
                error=str(exc),
            )
        return await render_api_clients_page(
            request,
            admin=admin,
            hub=hub,
            issued_key=issued.api_key,
            issued_client=issued.client,
        )


@router.post("/admin/api-clients/{client_id}/rotate")
async def api_client_rotate(request: Request, client_id: str):
    form = await request.form()
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            issued = await hub.external_api.rotate_key(client_id, admin_id=admin.id)
        except (ConflictError, NotFoundError) as exc:
            set_flash(request, str(exc), "danger")
            return RedirectResponse("/admin/api-clients", status_code=303)
        return await render_api_clients_page(
            request,
            admin=admin,
            hub=hub,
            issued_key=issued.api_key,
            issued_client=issued.client,
        )


@router.post("/admin/api-clients/{client_id}/toggle")
async def api_client_toggle(request: Request, client_id: str):
    form = await request.form()
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            client = await hub.external_api.get_client(client_id)
            await hub.external_api.set_active(
                client.id,
                is_active=not client.is_active,
                admin_id=admin.id,
            )
        except (ConflictError, NotFoundError) as exc:
            set_flash(request, str(exc), "danger")
        else:
            set_flash(request, "Статус API-клиента изменён.")
    return RedirectResponse("/admin/api-clients", status_code=303)


@router.post("/admin/api-clients/{client_id}/scopes")
async def api_client_scopes_update(request: Request, client_id: str):
    form = await request.form()
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            await hub.external_api.update_scopes(
                client_id,
                scopes=list(form.getlist("scopes")),
                admin_id=admin.id,
            )
        except (ConflictError, NotFoundError) as exc:
            set_flash(request, str(exc), "danger")
        else:
            set_flash(request, "Разрешения API-клиента обновлены.")
    return RedirectResponse("/admin/api-clients", status_code=303)


@router.post("/admin/api-clients/{client_id}/revoke")
async def api_client_revoke(request: Request, client_id: str):
    form = await request.form()
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            await hub.external_api.revoke_client(client_id, admin_id=admin.id)
        except NotFoundError as exc:
            set_flash(request, str(exc), "danger")
        else:
            set_flash(request, "API-клиент отозван. Его ключ больше не работает.")
    return RedirectResponse("/admin/api-clients", status_code=303)


@router.get("/admin/api-docs")
async def api_docs_page(request: Request):
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        return render(
            request,
            "api_docs.html",
            title="Документация API",
            admin=admin,
            scope_definitions=EXTERNAL_API_SCOPE_DEFINITIONS,
            api_base_url=f"{external_api_base_url(request)}/api/external/v1",
            active_nav="api",
        )


@router.get("/admin/events")
async def events_page(request: Request):
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        events = await hub.dashboard.list_events()
        return render(
            request,
            "events.html",
            title="Системные события",
            admin=admin,
            events=events,
            active_nav="events",
        )


@router.get("/admin/support")
async def admin_support_page(
    request: Request,
    status_filter: str = "open",
    request_id: str | None = None,
):
    normalized_status = status_filter.strip().lower()
    if normalized_status == "resolved":
        query_status = SupportRequestStatus.RESOLVED
    elif normalized_status == "all":
        query_status = None
    else:
        normalized_status = "open"
        query_status = SupportRequestStatus.NEW

    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        items = await hub.support.list_requests(status=query_status, limit=100)
        selected = next((item for item in items if item.id == request_id), None)
        if selected is None and request_id:
            try:
                selected = await hub.support.get_request(request_id)
            except NotFoundError:
                selected = None
        if selected is None and items:
            selected = items[0]
        return render(
            request,
            "support.html",
            title="Поддержка",
            admin=admin,
            support_requests=items,
            selected_support_request=selected,
            support_status_filter=normalized_status,
            active_nav="support",
        )


@router.post("/admin/support/{request_id}/reply")
async def admin_support_reply(request: Request, request_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    message = str(form.get("message") or "").strip()
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            await hub.support.add_admin_message(
                request_id,
                admin_id=admin.id,
                message=message,
            )
            item = await hub.support.get_request(request_id)
            await hub.notifications.queue(
                user_id=item.user_id,
                notification_type=NotificationType.BROADCAST,
                message=(
                    "💬 Поддержка ALTLINK ответила на ваш запрос.\n\n"
                    f"{message}\n\n"
                    "Вы можете ответить кнопкой ниже или открыть чат в личном кабинете."
                ),
                payload={
                    "cta": "support_reply",
                    "support_request_id": item.id,
                },
            )
            set_flash(request, "Ответ сохранён и поставлен в очередь на отправку пользователю.")
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
    return RedirectResponse(f"/admin/support?request_id={request_id}", status_code=303)


@router.post("/admin/support/{request_id}/resolve")
async def admin_support_resolve(request: Request, request_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    resolution_comment = str(form.get("resolution_comment") or "").strip() or None
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        try:
            await hub.support.resolve_request(
                request_id,
                admin_id=admin.id,
                resolution_comment=resolution_comment,
            )
            set_flash(request, "Обращение закрыто.")
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
    return RedirectResponse(f"/admin/support?request_id={request_id}", status_code=303)


@router.get("/support/attachments/{message_id}")
async def support_attachment(request: Request, message_id: str):
    async with request.app.state.container.hub() as hub:
        item = await hub.session.get(
            SupportMessage,
            message_id,
            options=[joinedload(SupportMessage.support_request)],
        )
        if item is None or not item.attachment_path:
            raise HTTPException(status_code=404, detail="Вложение не найдено.")

        admin = await resolve_admin(request, hub)
        portal_user = None if admin is not None else await resolve_portal_user(request, hub)
        is_owner = bool(
            portal_user is not None
            and item.support_request is not None
            and item.support_request.user_id == portal_user.id
        )
        if admin is None and not is_owner:
            raise HTTPException(status_code=404, detail="Вложение не найдено.")

        path = support_photo_path(request.app.state.settings, item.attachment_path)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="Вложение не найдено.")
        media_type = item.attachment_mime_type or "application/octet-stream"

    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/portal/login")
async def portal_login_page(request: Request):
    if request.session.get("portal_user_id"):
        return RedirectResponse("/portal", status_code=303)
    settings = request.app.state.settings
    login_enabled, login_issue, dev_login_enabled = portal_login_capabilities(settings)
    attempt = None
    deep_link = None
    qr_data_url = None
    if login_enabled:
        async with request.app.state.container.hub() as hub:
            attempt = await ensure_portal_login_attempt(request, hub)
        deep_link = portal_bot_login_url(settings, attempt.token) if attempt is not None else None
        qr_data_url = portal_login_qr_data_url(deep_link)
    return render(
        request,
        "portal_login.html",
        title="Вход в кабинет",
        telegram_login_bot=settings.client_bot_name.lstrip("@"),
        telegram_login_enabled=login_enabled,
        telegram_login_issue=login_issue,
        telegram_login_url=deep_link,
        telegram_login_qr_data_url=qr_data_url,
        telegram_login_status_url="/portal/login/status",
        telegram_login_attempt_expires_at=attempt.expires_at.isoformat() if attempt is not None else None,
        dev_login_enabled=dev_login_enabled,
    )


@router.get("/portal/login/status")
async def portal_login_status(request: Request) -> JSONResponse:
    token = request.session.get(PORTAL_LOGIN_ATTEMPT_SESSION_KEY)
    if not token:
        return JSONResponse(
            {"ok": False, "status": "missing", "message": "Попытка входа не найдена. Обновите страницу и начните заново."},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    async with request.app.state.container.hub() as hub:
        attempt = await hub.portal_auth.get_login_attempt(token)
        status_name = hub.portal_auth.login_attempt_status(attempt)
        if status_name == "approved":
            user = await hub.portal_auth.consume_login_attempt(token)
            request.session["portal_user_id"] = user.id
            request.session.pop(PORTAL_LOGIN_ATTEMPT_SESSION_KEY, None)
            return JSONResponse(
                {"ok": True, "status": "approved", "redirect_url": "/portal"},
                headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
            )

        if status_name in {"completed", "expired", "canceled", "missing"}:
            request.session.pop(PORTAL_LOGIN_ATTEMPT_SESSION_KEY, None)

        messages = {
            "pending": "Ожидаем подтверждение входа в Telegram.",
            "completed": "Попытка входа уже использована. Обновите страницу, чтобы начать заново.",
            "expired": "Время ожидания истекло. Обновите страницу, чтобы создать новый вход.",
            "canceled": "Вход был отменён в Telegram. Обновите страницу и попробуйте снова.",
            "missing": "Попытка входа не найдена. Обновите страницу и начните заново.",
        }
        return JSONResponse(
            {
                "ok": status_name == "pending",
                "status": status_name,
                "message": messages.get(status_name, "Не удалось определить состояние входа."),
                "expires_at": attempt.expires_at.isoformat() if attempt is not None else None,
            },
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )


@router.get("/portal/auth/telegram")
async def portal_telegram_auth(request: Request):
    payload = {key: value for key, value in request.query_params.items()}
    settings = request.app.state.settings
    if not verify_telegram_auth_payload(
        payload,
        bot_token=settings.client_bot_token,
        max_age_seconds=settings.telegram_auth_max_age_seconds,
    ):
        set_flash(request, "Не удалось подтвердить вход через Telegram.", "danger")
        return RedirectResponse("/portal/login", status_code=303)

    async with request.app.state.container.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=int(payload["id"]),
            username=payload.get("username"),
            first_name=payload.get("first_name"),
            last_name=payload.get("last_name"),
            language_code="ru",
        )
        request.session["portal_user_id"] = user.id
    set_flash(request, "Вход через Telegram подтверждён.")
    return RedirectResponse("/portal", status_code=303)


@router.post("/api/auth/telegram-webapp")
async def portal_telegram_webapp_auth(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    try:
        body = await request.json()
    except ValueError:
        body = {}
    init_data = str(body.get("init_data") or "")
    verified = verify_telegram_webapp_init_data(
        init_data,
        bot_token=settings.client_bot_token,
        max_age_seconds=settings.telegram_auth_max_age_seconds,
    )
    if verified is None:
        return JSONResponse(
            {"ok": False, "message": "Не удалось подтвердить вход через Telegram Mini App."},
            status_code=401,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    telegram_user = verified["user"]
    async with request.app.state.container.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=int(telegram_user["id"]),
            username=telegram_user.get("username"),
            first_name=telegram_user.get("first_name"),
            last_name=telegram_user.get("last_name"),
            language_code=telegram_user.get("language_code") or "ru",
        )
        request.session["portal_user_id"] = user.id
        request.session.pop(PORTAL_LOGIN_ATTEMPT_SESSION_KEY, None)
    return JSONResponse(
        {"ok": True, "redirect_url": "/portal"},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@router.post("/portal/dev-login")
async def portal_dev_login(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    settings = request.app.state.settings
    _, _, dev_login_enabled = portal_login_capabilities(settings)
    if not dev_login_enabled:
        set_flash(request, "Локальный вход отключён. Подтвердите вход через Telegram-бота.", "danger")
        return RedirectResponse("/portal/login", status_code=303)

    try:
        telegram_id = int(str(form.get("telegram_id", "")).strip())
    except ValueError:
        set_flash(request, "Укажите корректный Telegram ID.", "danger")
        return RedirectResponse("/portal/login", status_code=303)

    if telegram_id <= 0:
        set_flash(request, "Telegram ID должен быть положительным числом.", "danger")
        return RedirectResponse("/portal/login", status_code=303)

    async with request.app.state.container.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=telegram_id,
            username=None,
            first_name="Локальный",
            last_name="вход",
            language_code="ru",
        )
        request.session["portal_user_id"] = user.id
    set_flash(request, "Локальный вход выполнен. Этот режим предназначен только для разработки.")
    return RedirectResponse("/portal", status_code=303)


@router.post("/portal/logout")
async def portal_logout(request: Request):
    request.session.pop("portal_user_id", None)
    set_flash(request, "Вы вышли из личного кабинета.")
    return RedirectResponse("/portal/login", status_code=303)


@router.get("/portal")
async def portal_home(request: Request):
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
        context = await build_portal_context(request, hub, user)
        return render(request, "portal_dashboard.html", **context)


@router.post("/portal/devices/delete")
async def portal_device_delete(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    hwid = str(form.get("hwid", "")).strip()
    if not hwid:
        set_flash(request, "Устройство не выбрано.", "danger")
        return RedirectResponse("/portal", status_code=303)
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
        try:
            await hub.accounts.delete_user_hwid_device(user.id, hwid)
            set_flash(request, "Устройство удалено.")
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
    return RedirectResponse("/portal", status_code=303)


@router.post("/portal/trial")
async def portal_trial(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
        if not await portal_channel_state(request, user):
            set_flash(request, "Сначала подпишитесь на Telegram-канал проекта.", "danger")
            return RedirectResponse("/portal", status_code=303)
        try:
            await hub.billing.activate_trial(user.id)
            set_flash(request, "Тестовый период на 2 дня активирован.")
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
    return RedirectResponse("/portal", status_code=303)


@router.post("/portal/plan")
async def portal_plan(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    plan_code = parse_paid_plan_code(form.get("plan_code"))
    if plan_code is None:
        set_flash(request, "Тариф больше не поддерживается. Обновите страницу и выберите один из актуальных тарифов.", "danger")
        return RedirectResponse("/portal", status_code=303)
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
        if not await portal_channel_state(request, user):
            set_flash(request, "Сначала подпишитесь на Telegram-канал проекта.", "danger")
            return RedirectResponse("/portal", status_code=303)
        try:
            await hub.billing.activate_paid_plan(user.id, plan_code, charge_user=True)
            set_flash(request, "Тариф успешно активирован.")
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
    return RedirectResponse("/portal", status_code=303)


@router.post("/portal/whitelist-package")
async def portal_whitelist_package(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    package_code = str(form.get("package_code") or "")
    request_key = str(form.get("request_key") or "") or secrets.token_hex(16)
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
        try:
            purchase = await hub.billing.purchase_whitelist_package(
                user.id,
                package_code,
                request_key=request_key,
            )
            set_flash(request, f"Пакет +{purchase.traffic_bytes // 1024**3} ГБ успешно куплен.")
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
    return RedirectResponse("/portal#portal-subscription", status_code=303)


def whitelist_status_payload(status_value) -> dict[str, object]:
    return {
        "billing_version": status_value.billing_version,
        "legacy": status_value.legacy,
        "included_limit_bytes": status_value.included_limit_bytes,
        "included_used_bytes": status_value.included_used_bytes,
        "included_remaining_bytes": status_value.included_remaining_bytes,
        "extra_remaining_bytes": status_value.extra_remaining_bytes,
        "paid_bytes": status_value.paid_bytes,
        "paid_cost_rub": str(status_value.paid_cost_rub),
        "total_used_bytes": status_value.total_used_bytes,
        "price_per_gb_rub": str(status_value.price_per_gb_rub),
        "balance_rub": str(status_value.balance_rub),
        "access_allowed": status_value.access_allowed,
        "period_starts_at": (
            status_value.period_starts_at.isoformat() if status_value.period_starts_at is not None else None
        ),
        "period_ends_at": status_value.period_ends_at.isoformat() if status_value.period_ends_at is not None else None,
    }


@router.get("/api/whitelist/traffic")
async def portal_whitelist_traffic_api(request: Request) -> JSONResponse:
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return JSONResponse({"success": False, "message": "Нужно войти в кабинет."}, status_code=401)
        status_value = await hub.billing.get_whitelist_traffic_status(user.id)
    return JSONResponse({"success": True, "traffic": whitelist_status_payload(status_value)})


@router.get("/api/whitelist/packages")
async def portal_whitelist_packages_api(request: Request) -> JSONResponse:
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return JSONResponse({"success": False, "message": "Нужно войти в кабинет."}, status_code=401)
    packages = [
        {
            "code": code,
            "gigabytes": int(item["gigabytes"]),
            "traffic_bytes": int(item["gigabytes"]) * 1024**3,
            "price_rub": str(item["price_rub"]),
            "regular_price_rub": str(Decimal(item["gigabytes"]) * WHITELIST_GB_PRICE_RUB),
        }
        for code, item in WHITELIST_TRAFFIC_PACKAGES.items()
    ]
    return JSONResponse({"success": True, "packages": packages, "price_per_gb_rub": str(WHITELIST_GB_PRICE_RUB)})


@router.get("/api/whitelist/purchases")
async def portal_whitelist_purchases_api(request: Request) -> JSONResponse:
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return JSONResponse({"success": False, "message": "Нужно войти в кабинет."}, status_code=401)
        rows = await hub.billing.list_whitelist_package_purchases(user.id, limit=100)
    return JSONResponse(
        {
            "success": True,
            "purchases": [
                {
                    "id": item.id,
                    "package_code": item.package_code,
                    "traffic_bytes": item.traffic_bytes,
                    "price_rub": str(item.price_rub),
                    "status": item.status,
                    "created_at": item.created_at.isoformat(),
                    "kind": "admin_grant" if item.created_by_admin_id else "purchase",
                }
                for item in rows
            ],
        }
    )


@router.post("/api/whitelist/purchases")
async def portal_whitelist_purchase_api(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    provided_csrf = str(body.get("csrf_token") or "")
    session_csrf = str(request.session.get("csrf_token") or "")
    if not provided_csrf or not session_csrf or not secrets.compare_digest(provided_csrf, session_csrf):
        return JSONResponse({"success": False, "message": "Некорректный CSRF токен."}, status_code=400)
    package_code = str(body.get("package_code") or "")
    request_key = str(body.get("request_key") or "") or secrets.token_hex(16)
    if len(request_key) > 64:
        return JSONResponse({"success": False, "message": "Некорректный ключ операции."}, status_code=400)
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return JSONResponse({"success": False, "message": "Нужно войти в кабинет."}, status_code=401)
        try:
            purchase = await hub.billing.purchase_whitelist_package(
                user.id,
                package_code,
                request_key=request_key,
            )
        except (ConflictError, NotFoundError, ServiceError) as exc:
            return JSONResponse({"success": False, "message": str(exc)}, status_code=409)
        status_value = await hub.billing.get_whitelist_traffic_status(user.id)
    return JSONResponse(
        {
            "success": True,
            "purchase_id": purchase.id,
            "traffic": whitelist_status_payload(status_value),
        }
    )


@router.post("/portal/link/revoke")
async def portal_link_revoke(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    if form.get("confirm") != "1":
        set_flash(request, "Подтвердите перевыпуск ссылки.", "danger")
        return RedirectResponse("/portal#portal-subscription", status_code=303)
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
        try:
            await hub.accounts.revoke_user_subscription_link(user.id)
            request.session["portal_link_reissued"] = True
            set_flash(request, "Ссылка перевыпущена. Старую ссылку нужно заменить в приложении.")
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
    return RedirectResponse("/portal#portal-subscription", status_code=303)


@router.get("/portal/vless-keys")
async def portal_vless_keys(request: Request):
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
        subscription = await hub.accounts.get_current_subscription(user.id)
        if subscription is None:
            set_flash(request, "Сначала активируйте тариф.", "danger")
            return RedirectResponse("/portal#portal-subscription", status_code=303)
        try:
            keys = await hub.accounts.get_rate_limited_user_vless_keys(user.id)
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
            return RedirectResponse("/portal#portal-subscription", status_code=303)
    return Response(
        vless_keys_file_content(keys),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="altlink-vless-keys.txt"'},
    )


@router.post("/portal/topup")
async def portal_topup(request: Request):
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
    set_flash(request, "Пополнение через сайт пока скрыто. Используйте клиентский бот.", "warning")
    return RedirectResponse("/portal", status_code=303)


@router.post("/api/payments/create")
async def portal_payment_create(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if body.get("csrf_token") != request.session.get("csrf_token"):
        return JSONResponse({"success": False, "message": "Некорректный CSRF токен."}, status_code=400)

    try:
        amount = Decimal(str(body.get("amount") or "0"))
    except Exception:
        return JSONResponse({"success": False, "message": "Введите корректную сумму."}, status_code=400)
    if not amount.is_finite():
        return JSONResponse({"success": False, "message": "Введите корректную сумму."}, status_code=400)
    if amount <= 0:
        return JSONResponse({"success": False, "message": "Введите сумму пополнения."}, status_code=400)

    source = str(body.get("source") or "portal").strip()[:32] or "portal"
    promo_code = str(body.get("promo_code") or "").strip()[:64]
    comment = source if not promo_code else f"{source}; promo={promo_code}"

    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return JSONResponse({"success": False, "message": "Нужно войти в кабинет."}, status_code=401)
        try:
            checkout = await hub.topups.create_checkout(user.id, amount, comment=comment)
        except (ConflictError, NotFoundError, ServiceError) as exc:
            return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

        if checkout.payment_url:
            return JSONResponse(
                {
                    "success": True,
                    "payment_id": checkout.request.id,
                    "confirmation_url": checkout.payment_url,
                    "payment_url": checkout.payment_url,
                    "provider": checkout.provider,
                }
            )
        if checkout.auto_completed:
            return JSONResponse(
                {
                    "success": True,
                    "payment_id": checkout.request.id,
                    "confirmation_url": None,
                    "provider": checkout.provider,
                    "message": f"Баланс пополнен на {format_rub_amount(amount)} ₽.",
                }
            )
        return JSONResponse(
            {
                "success": False,
                "payment_id": checkout.request.id,
                "confirmation_url": None,
                "provider": checkout.provider,
                "message": "Онлайн-оплата сейчас недоступна. Попробуйте позже или используйте бот.",
            },
            status_code=409,
        )


@router.post("/api/promo/apply")
async def portal_promo_apply(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if body.get("csrf_token") != request.session.get("csrf_token"):
        return JSONResponse({"success": False, "message": "Некорректный CSRF токен."}, status_code=400)
    code = str(body.get("code") or "").strip()
    if not code:
        return JSONResponse({"success": False, "message": "Введите промокод."}, status_code=400)

    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return JSONResponse({"success": False, "message": "Нужно войти в кабинет."}, status_code=401)
        try:
            promo, redemption, result_message = await hub.billing.redeem_promo_code(user.id, code)
        except (ConflictError, NotFoundError, ServiceError) as exc:
            return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

        if promo.reward_kind == PromoRewardKind.BALANCE:
            applied = redemption.reward_value_applied or promo.reward_value
            message = f"Промокод применён. На баланс зачислено {format_rub_amount(applied)} ₽."
            payload = {"balance_delta": str(applied), "bonus": str(applied)}
        elif promo.reward_kind == PromoRewardKind.PLAN_DISCOUNT:
            message = f"Промокод применён. Скидка {format_rub_amount(promo.reward_value)}% появится в ценах тарифа."
            payload = {"discount": str(promo.reward_value)}
        else:
            message = result_message
            payload = {"trial_days": int(promo.reward_value), "trial_activated": True}
    return JSONResponse({"success": True, "message": message, **payload})


@router.post("/portal/support")
async def portal_support_create(request: Request):
    form_data = await request.form()
    form = dict(form_data)
    validate_csrf(request, form)
    topic = str(form.get("topic") or "Обращение").strip()[:64] or "Обращение"
    message = str(form.get("message") or "").strip()
    settings = request.app.state.settings
    created_request = None
    attachment = None
    admin_ids: list[int] = []
    user = None
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
        try:
            attachment = await save_support_photo(form.get("photo"), settings)
            created_request = await hub.support.create_request(
                user_id=user.id,
                topic=topic,
                message=message,
                **support_attachment_kwargs(attachment),
            )
            admin_ids = await hub.accounts.list_admin_telegram_ids()
            set_flash(request, "Запрос отправлен. Ответ появится в чате поддержки.")
        except (ConflictError, NotFoundError, ServiceError) as exc:
            delete_support_photo(settings, attachment)
            set_flash(request, str(exc), "danger")
            return RedirectResponse("/portal#portal-home", status_code=303)
    if created_request is not None and user is not None:
        await notify_admins_about_support_request_from_portal(
            settings,
            admin_telegram_ids=admin_ids,
            user=user,
            request_id=created_request.id,
            topic=topic,
            message=created_request.message,
            photo_path=attachment.get("absolute_path") if attachment else None,
        )
    return RedirectResponse("/portal#portal-home", status_code=303)


@router.post("/portal/support/{request_id}/messages")
async def portal_support_message(request: Request, request_id: str):
    form_data = await request.form()
    form = dict(form_data)
    validate_csrf(request, form)
    message = str(form.get("message") or "").strip()
    settings = request.app.state.settings
    attachment = None
    added_message = None
    item = None
    admin_ids: list[int] = []
    user = None
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
        try:
            attachment = await save_support_photo(form.get("photo"), settings)
            added_message = await hub.support.add_user_message(
                request_id,
                user_id=user.id,
                message=message,
                **support_attachment_kwargs(attachment),
            )
            item = await hub.support.get_request(request_id)
            admin_ids = await hub.accounts.list_admin_telegram_ids()
            set_flash(request, "Сообщение отправлено.")
        except (ConflictError, NotFoundError, ServiceError) as exc:
            delete_support_photo(settings, attachment)
            set_flash(request, str(exc), "danger")
    if added_message is not None and item is not None and user is not None:
        await notify_admins_about_support_request_from_portal(
            settings,
            admin_telegram_ids=admin_ids,
            user=user,
            request_id=item.id,
            topic=item.topic,
            message=added_message.message,
            heading="Новое сообщение в обращении с сайта",
            photo_path=attachment.get("absolute_path") if attachment else None,
        )
    return RedirectResponse("/portal#portal-home", status_code=303)


@router.get("/portal/check-channel")
async def portal_check_channel(request: Request):
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
        if await portal_channel_state(request, user):
            set_flash(request, "Подписка на канал подтверждена.")
        else:
            set_flash(request, "Подписка на канал пока не найдена.", "danger")
    return RedirectResponse("/portal", status_code=303)

