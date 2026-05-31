from __future__ import annotations

import asyncio
import logging
import base64
import json
import re
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from altlink.domain.billing import bytes_to_gb_cost
from altlink.domain.enums import BalanceTransactionType, PlanCode, ServerType, TopupStatus
from altlink.domain.plans import is_metered_plan_code, parse_paid_plan_code
from altlink.infrastructure.db.models import Subscription, SystemSetting, User
from altlink.application.services.base import ConflictError, NotFoundError, ServiceError
from altlink.application.services.monitoring import MonitoringService
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
from altlink.utils.telegram_web import check_channel_membership, verify_telegram_auth_payload

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


def build_landing_latency_items(servers, latency_state: dict[str, dict]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for server in servers or []:
        if not getattr(server, "is_available", False):
            continue
        latency = latency_state.get(getattr(server, "id", "")) if isinstance(latency_state, dict) else None
        label, state = landing_latency_label(latency)
        latency_ms = latency.get("latency_ms") if isinstance(latency, dict) else None
        items.append(
            {
                "server_id": getattr(server, "id", None),
                "name": getattr(server, "name", None),
                "country_code": (getattr(server, "country_code", "") or "").upper(),
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
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "csrf_token": get_csrf_token(request),
            "flash": pop_flash(request),
            **context,
        },
    )


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
    portal_user_id = request.session.get("portal_user_id")
    if not portal_user_id:
        return None
    try:
        return await hub.accounts.get_user(portal_user_id)
    except Exception:
        request.session.pop("portal_user_id", None)
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
    return path.read_text(encoding="utf-8-sig").strip()


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

    qr_data_uri = None
    info = bundle.get("subscription_info")
    payload = bundle.get("subscription_url") or (info.subscriptionUrl if info else None)
    if payload:
        qr_png = render_qr_png(payload)
        qr_data_uri = f"data:image/png;base64,{base64.b64encode(qr_png).decode('ascii')}"

    current_plan = subscription.plan if subscription else None
    current_plan_code = getattr(current_plan.code, "value", current_plan.code) if current_plan else None

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
        "portal_url": f"{request.app.state.settings.backend_public_url.rstrip('/')}/portal",
        "client_bot_url": f"https://t.me/{request.app.state.settings.client_bot_name.lstrip('@')}" if request.app.state.settings.client_bot_name else None,
        "connection_help_url": f"{request.app.state.settings.backend_public_url.rstrip('/')}/help/connect",
        "agreement_page_url": f"{request.app.state.settings.backend_public_url.rstrip('/')}/legal/agreement",
        "privacy_page_url": f"{request.app.state.settings.backend_public_url.rstrip('/')}/legal/privacy",
        "latency_api_url": "/api/latency",
        "latency_target_label": latency_target_label(),
        "latency_disclaimer": latency_disclaimer_text(),
        "required_channel_url": request.app.state.settings.required_subscription_channel_url,
        "whitelist_price_per_gb": request.app.state.settings.whitelist_price_per_gb_rub,
        "whitelist_cost_rub": bytes_to_gb_cost(
            subscription.whitelist_traffic_used_bytes if subscription else 0,
            Decimal(request.app.state.settings.whitelist_price_per_gb_rub),
        ),
        "telegram_login_bot": request.app.state.settings.client_bot_name.lstrip("@"),
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
    settings = request.app.state.settings
    landing_latency_items = build_landing_latency_items(servers, server_latency_state)
    landing_latency_values = [
        item["latency_ms"]
        for item in landing_latency_items
        if isinstance(item.get("latency_ms"), int | float)
    ]
    landing_latency_best_label = f"{round(min(landing_latency_values))} мс" if landing_latency_values else "—"
    landing_latency_initial_hint = (
        "Показываем последние сохранённые значения по серверам. Кнопка ниже обновит замер из вашего браузера."
        if landing_latency_items
        else "Нажмите кнопку, чтобы запустить измерение."
    )
    return render(
        request,
        "landing.html",
        title="ALTLINK",
        portal_plan_groups=group_portal_plans(plans),
        portal_login_url="/portal/login?autostart=1",
        connection_help_url=f"{settings.backend_public_url.rstrip('/')}/help/connect",
        agreement_page_url=f"{settings.backend_public_url.rstrip('/')}/legal/agreement",
        privacy_page_url=f"{settings.backend_public_url.rstrip('/')}/legal/privacy",
        client_bot_url=f"https://t.me/{settings.client_bot_name.lstrip('@')}" if settings.client_bot_name else None,
        support_username=settings.support_username,
        latency_api_url="/api/latency",
        latency_target_label=latency_target_label(),
        latency_disclaimer=latency_disclaimer_text(),
        landing_latency_items=landing_latency_items,
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
        document_excerpt=document_text.splitlines()[0] if document_text else "",
    )


@router.get("/legal/privacy")
async def privacy_page(request: Request):
    document_text = load_document_text("privacy")
    return render(
        request,
        "legal_privacy.html",
        title="Политика конфиденциальности",
        document_html=markdown_to_html(document_text),
        document_excerpt=document_text.splitlines()[0] if document_text else "",
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
async def dashboard(request: Request):
    container = request.app.state.container
    async with container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
    await sync_dashboard_traffic_if_possible(container)
    async with container.hub() as hub:
        overview = await hub.dashboard.overview()
        return render(
            request,
            "dashboard.html",
            title="Dashboard",
            admin=admin,
            overview=overview,
            charts=overview["charts"],
            charts_json=json.dumps(overview["charts"], ensure_ascii=False),
            active_nav="dashboard",
        )


@router.get("/admin/users")
async def users_page(request: Request, search: str | None = None):
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        users = await hub.accounts.list_users(search)
        return render(
            request,
            "users.html",
            title="Пользователи",
            admin=admin,
            users=users,
            search=search or "",
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
            whitelist_cost_rub=bytes_to_gb_cost(
                card["subscription"].whitelist_traffic_used_bytes if card["subscription"] else 0,
                Decimal(request.app.state.settings.whitelist_price_per_gb_rub),
            ),
            active_nav="users",
        )


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
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        await sync_server_catalog_if_possible(hub)
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


@router.post("/portal/topup")
async def portal_topup(request: Request):
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
    set_flash(request, "Пополнение через сайт пока скрыто. Используйте клиентский бот.", "warning")
    return RedirectResponse("/portal", status_code=303)


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

