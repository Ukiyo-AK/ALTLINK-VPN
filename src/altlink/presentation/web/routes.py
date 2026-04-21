from __future__ import annotations

import base64
import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from altlink.domain.billing import bytes_to_gb_cost
from altlink.domain.enums import BalanceTransactionType, ServerType, TopupStatus
from altlink.domain.plans import parse_paid_plan_code
from altlink.infrastructure.db.models import Subscription, SystemSetting, User
from altlink.application.services.base import ConflictError, NotFoundError, ServiceError
from altlink.utils.qr import render_qr_png
from altlink.utils.security import generate_token
from altlink.utils.telegram_web import check_channel_membership, verify_telegram_auth_payload

router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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
    parsed = urlparse(settings.backend_public_url)
    host = (parsed.hostname or "").lower()
    scheme = (parsed.scheme or "").lower()
    local_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

    if not host:
        return (
            False,
            "Не задан BACKEND_PUBLIC_URL. Укажите публичный адрес сайта, чтобы Telegram Login Widget смог работать.",
            True,
        )
    if host in local_hosts:
        return (
            False,
            "Telegram Login Widget не работает с localhost и локальными IP. Для локальной разработки используйте вход по Telegram ID ниже.",
            True,
        )
    if scheme != "https":
        return (
            False,
            "Telegram Login Widget требует публичный HTTPS-домен. Переключите BACKEND_PUBLIC_URL на https://ваш-домен.",
            settings.debug,
        )
    return True, None, settings.debug


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


async def build_portal_context(request: Request, hub, user: User) -> dict:
    bundle = await hub.accounts.get_subscription_bundle(user.id)
    subscription = bundle.get("subscription")
    user_servers = await hub.catalog.get_user_servers(user.id)
    plans = await hub.dashboard.list_plans()
    payments = await hub.topups.list_requests(user_id=user.id)
    channel_ok = await portal_channel_state(request, user)

    qr_data_uri = None
    info = bundle.get("subscription_info")
    keys = bundle.get("connection_keys")
    payload = info.subscriptionUrl if info else None
    if payload is None and keys and keys.enabledKeys:
        payload = keys.enabledKeys[0]
    if payload:
        qr_png = render_qr_png(payload)
        qr_data_uri = f"data:image/png;base64,{base64.b64encode(qr_png).decode('ascii')}"

    return {
        "title": "Личный кабинет",
        "portal_user": user,
        "portal_subscription": subscription,
        "portal_bundle": bundle,
        "portal_servers": user_servers,
        "portal_plans": plans,
        "portal_payments": payments,
        "portal_qr_data_uri": qr_data_uri,
        "portal_channel_ok": channel_ok,
        "portal_url": f"{request.app.state.settings.backend_public_url.rstrip('/')}/portal",
        "required_channel_url": request.app.state.settings.required_subscription_channel_url,
        "whitelist_price_per_gb": request.app.state.settings.whitelist_price_per_gb_rub,
        "whitelist_cost_rub": bytes_to_gb_cost(
            subscription.whitelist_traffic_used_bytes if subscription else 0,
            Decimal(request.app.state.settings.whitelist_price_per_gb_rub),
        ),
        "telegram_login_bot": request.app.state.settings.client_bot_name.lstrip("@"),
    }


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
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        overview = await hub.dashboard.overview()
        return render(
            request,
            "dashboard.html",
            title="Dashboard",
            admin=admin,
            overview=overview,
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
        return render(
            request,
            "user_detail.html",
            title="Карточка пользователя",
            admin=admin,
            card=card,
            bundle=bundle,
            user_servers=user_servers,
            plans=plans,
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
        servers = await hub.catalog.list_servers()
        return render(
            request,
            "servers.html",
            title="Серверы",
            admin=admin,
            servers=servers,
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
        await hub.catalog.sync_servers()
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
        await hub.catalog.set_server_availability(server_id, available)
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
        await hub.catalog.set_server_capacity(server_id, max_clients)
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
        await hub.catalog.set_server_type(server_id, ServerType(raw_type))
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
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        subscriptions = list(
            (
                await hub.session.scalars(
                    select(Subscription).options(joinedload(Subscription.user), joinedload(Subscription.plan))
                )
            ).all()
        )
        subscriptions.sort(key=lambda item: item.traffic_used_bytes, reverse=True)
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
        return render(
            request,
            "settings.html",
            title="Настройки",
            admin=admin,
            settings_list=settings_list,
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
    widget_enabled, widget_issue, dev_login_enabled = portal_login_capabilities(settings)
    return render(
        request,
        "portal_login.html",
        title="Вход в кабинет",
        telegram_login_bot=settings.client_bot_name.lstrip("@"),
        backend_public_url=settings.backend_public_url.rstrip("/"),
        telegram_widget_enabled=widget_enabled,
        telegram_widget_issue=widget_issue,
        dev_login_enabled=dev_login_enabled,
        portal_domain=urlparse(settings.backend_public_url).hostname or settings.backend_public_url,
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
        set_flash(request, "Локальный вход отключён. Используйте Telegram Login Widget.", "danger")
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
    form = dict(await request.form())
    validate_csrf(request, form)
    amount = Decimal(str(form.get("amount", "0")))
    async with request.app.state.container.hub() as hub:
        user = await resolve_portal_user(request, hub)
        if user is None:
            return portal_login_redirect()
        await hub.topups.create_request(user.id, amount, auto_complete=True)
        set_flash(request, f"Баланс пополнен на {amount:.2f} ₽ через тестовую заглушку.")
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
