from __future__ import annotations

import asyncio
import logging
import base64
import json
import re
from datetime import UTC, datetime, time
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.parse import unquote, urlparse

from aiogram import Bot
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select
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
    UserStatus,
)
from altlink.domain.notifications import (
    PROMO_MESSAGE_TEMPLATES,
    promo_template_kind,
    render_promo_campaign_message,
)
from altlink.domain.plans import is_metered_plan_code, parse_paid_plan_code
from altlink.infrastructure.db.models import (
    BalanceTransaction,
    Notification,
    PromoCode,
    PromoCodeRedemption,
    Subscription,
    SystemSetting,
    User,
)
from altlink.application.services.billing import DEFAULT_PROMO_CAMPAIGN_SETTINGS, PROMO_CAMPAIGN_SETTINGS_KEY
from altlink.application.services.base import ConflictError, NotFoundError, ServiceError
from altlink.application.services.monitoring import MonitoringService
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
ASSET_VERSION = "20260613-dashboard-analytics"
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
            groups[family] = {
                "family": family,
                "title": "Start" if family == "10gbit" else "Pro",
                "description": plan.description,
                "device_limit": plan.device_limit,
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


async def notify_admins_about_support_request_from_portal(
    settings,
    *,
    admin_telegram_ids: list[int],
    user: User,
    request_id: str,
    topic: str,
    message: str,
) -> None:
    if not admin_telegram_ids or not settings.admin_bot_token:
        return
    text = "\n".join(
        [
            "Новый запрос поддержки с сайта",
            "",
            f"Пользователь: @{user.username}" if user.username else f"Пользователь: Telegram ID {user.telegram_id}",
            f"Telegram ID: {user.telegram_id}",
            f"Тема: {topic}",
            f"Номер: {request_id}",
            "",
            message,
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
        support_requests[0] if support_requests else None,
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
        "whitelist_price_per_gb": settings.whitelist_price_per_gb_rub,
        "whitelist_cost_rub": bytes_to_gb_cost(
            subscription.whitelist_traffic_used_bytes if subscription else 0,
            Decimal(settings.whitelist_price_per_gb_rub),
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
    if refresh:
        async with container.hub() as sync_hub:
            await sync_server_catalog_if_possible(sync_hub)
        await sync_dashboard_traffic_if_possible(container)
    async with container.hub() as hub:
        overview = await hub.dashboard.overview(period=period)
        return render(
            request,
            "dashboard.html",
            title="Dashboard",
            admin=admin,
            overview=overview,
            charts=overview["charts"],
            charts_json=json.dumps(overview["charts"], ensure_ascii=False),
            selected_period=overview["period"],
            refresh_requested=refresh,
            active_nav="dashboard",
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
        plans = await hub.dashboard.list_plans()
        devices, devices_error = await safe_user_hwid_device_views(hub, user_id)
        has_paid_history = await hub.accounts.has_paid_subscription_history(user_id)
        promo_setting = await hub.session.scalar(
            select(SystemSetting).where(SystemSetting.key == PROMO_CAMPAIGN_SETTINGS_KEY)
        )
        promo_settings = normalize_promo_campaign_settings(promo_setting.value if promo_setting is not None else None)
        return render(
            request,
            "user_detail.html",
            title="Карточка пользователя",
            admin=admin,
            card=card,
            bundle=bundle,
            user_servers=user_servers,
            plans=plans,
            devices=devices,
            devices_error=devices_error,
            promo_template_options=promo_template_options(),
            promo_default_discount=(
                promo_settings["lapsed_user_discount_percent"]
                if has_paid_history
                else promo_settings["new_user_discount_percent"]
            ),
            whitelist_cost_rub=bytes_to_gb_cost(
                card["subscription"].whitelist_traffic_used_bytes if card["subscription"] else 0,
                Decimal(request.app.state.settings.whitelist_price_per_gb_rub),
            ),
            active_nav="users",
        )


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
async def transactions_page(request: Request):
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        transactions = await hub.dashboard.list_transactions()
        return render(
            request,
            "transactions.html",
            title="Баланс и транзакции",
            admin=admin,
            transactions=transactions,
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
            whitelist_price_per_gb=request.app.state.settings.whitelist_price_per_gb_rub,
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
            promo, redemption, _ = await hub.promos.redeem_code(user.id, code)
        except (ConflictError, NotFoundError, ServiceError) as exc:
            return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

        if promo.reward_kind == PromoRewardKind.BALANCE:
            applied = redemption.reward_value_applied or promo.reward_value
            message = f"Промокод применён. На баланс зачислено {format_rub_amount(applied)} ₽."
            payload = {"balance_delta": str(applied), "bonus": str(applied)}
        else:
            message = f"Промокод применён. Скидка {format_rub_amount(promo.reward_value)}% появится в ценах тарифа."
            payload = {"discount": str(promo.reward_value)}
    return JSONResponse({"success": True, "message": message, **payload})


@router.post("/portal/support")
async def portal_support_create(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    topic = str(form.get("topic") or "Обращение").strip()[:64] or "Обращение"
    message = str(form.get("message") or "").strip()
    settings = request.app.state.settings
    created_request = None
    admin_ids: list[int] = []
    user = None
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
        try:
            created_request = await hub.support.create_request(user_id=user.id, topic=topic, message=message)
            admin_ids = await hub.accounts.list_admin_telegram_ids()
            set_flash(request, "Запрос отправлен. Ответ появится в чате поддержки.")
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
            return RedirectResponse("/portal#portal-home", status_code=303)
    if created_request is not None and user is not None:
        await notify_admins_about_support_request_from_portal(
            settings,
            admin_telegram_ids=admin_ids,
            user=user,
            request_id=created_request.id,
            topic=topic,
            message=message,
        )
    return RedirectResponse("/portal#portal-home", status_code=303)


@router.post("/portal/support/{request_id}/messages")
async def portal_support_message(request: Request, request_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    message = str(form.get("message") or "").strip()
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
        try:
            await hub.support.add_user_message(request_id, user_id=user.id, message=message)
            set_flash(request, "Сообщение отправлено.")
        except (ConflictError, NotFoundError, ServiceError) as exc:
            set_flash(request, str(exc), "danger")
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

