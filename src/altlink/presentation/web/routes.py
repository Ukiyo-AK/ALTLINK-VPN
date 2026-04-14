from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from altlink.domain.enums import BalanceTransactionType, PlanCode, TopupStatus
from altlink.infrastructure.db.models import Subscription, SystemSetting, User
from altlink.utils.security import generate_token

router = APIRouter(prefix="/admin", tags=["admin-web"])
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


def render(request: Request, template_name: str, **context):
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "csrf_token": get_csrf_token(request),
            "admin_id": request.session.get("admin_id"),
            **context,
        },
    )


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


async def resolve_admin(request: Request, hub):
    admin_id = request.session.get("admin_id")
    if not admin_id:
        return None
    try:
        return await hub.accounts.get_admin(admin_id)
    except Exception:
        request.session.clear()
        return None


@router.get("/login")
async def login_page(request: Request, error: str | None = None):
    return render(request, "login.html", title="Вход", error=error)


@router.post("/login")
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


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


@router.get("/dashboard")
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
            active_nav="dashboard",
        )


@router.get("/users")
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


@router.get("/users/{user_id}")
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
            active_nav="users",
        )


@router.post("/users/{user_id}/balance")
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


@router.post("/users/{user_id}/trial")
async def user_trial(request: Request, user_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        await hub.billing.activate_trial(user_id)
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/plan")
async def user_plan(request: Request, user_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    plan_code = PlanCode(form.get("plan_code"))
    charge_user = form.get("charge_user") == "1"
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        await hub.billing.activate_paid_plan(user_id, plan_code, charge_user=charge_user, admin_id=admin.id)
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/activate")
async def user_activate(request: Request, user_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        await hub.billing.reactivate_user(user_id)
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/deactivate")
async def user_deactivate(request: Request, user_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        await hub.billing.deactivate_user(user_id)
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.get("/servers")
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
            active_nav="servers",
        )


@router.post("/servers/sync")
async def servers_sync(request: Request):
    form = dict(await request.form())
    validate_csrf(request, form)
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        await hub.catalog.sync_servers()
        return RedirectResponse("/admin/servers", status_code=303)


@router.post("/servers/{server_id}/toggle")
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


@router.post("/servers/{server_id}/capacity")
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


@router.get("/topups")
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
            title="Заявки на пополнение",
            admin=admin,
            topups=topups,
            status_filter=status_filter or "",
            active_nav="topups",
        )


@router.post("/topups/{request_id}/approve")
async def topup_approve(request: Request, request_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    comment = form.get("comment") or None
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        await hub.topups.approve(request_id, admin.id, comment)
        return RedirectResponse("/admin/topups", status_code=303)


@router.post("/topups/{request_id}/reject")
async def topup_reject(request: Request, request_id: str):
    form = dict(await request.form())
    validate_csrf(request, form)
    comment = form.get("comment") or None
    async with request.app.state.container.hub() as hub:
        admin = await resolve_admin(request, hub)
        if admin is None:
            return login_redirect()
        await hub.topups.reject(request_id, admin.id, comment)
        return RedirectResponse("/admin/topups", status_code=303)


@router.get("/plans")
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


@router.get("/transactions")
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


@router.get("/traffic")
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
            title="Трафик и лимиты",
            admin=admin,
            subscriptions=subscriptions,
            active_nav="traffic",
        )


@router.get("/online")
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


@router.get("/settings")
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


@router.post("/settings")
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


@router.get("/events")
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
