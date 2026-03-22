from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from altlink.application.services import (
    AccountService,
    AdminAuthService,
    BillingService,
    DashboardService,
    ServerService,
    SubscriptionService,
    TrafficService,
)
from altlink.infrastructure.db.models import (
    AdminUser,
    BalanceTransaction,
    Notification,
    OnlineSessionCache,
    Plan,
    Server,
    Subscription,
    SystemEvent,
    SystemSetting,
    TopupRequest,
    User,
)
from altlink.presentation.api.deps import get_remnawave, get_session, get_settings
from altlink.presentation.web.helpers import (
    admin_redirect,
    build_csrf_token,
    format_bytes,
    format_dt,
    format_money,
    verify_csrf,
)

router = APIRouter()
templates = Jinja2Templates(directory="src/altlink/presentation/web/templates")
templates.env.filters["money"] = format_money
templates.env.filters["bytesfmt"] = format_bytes
templates.env.filters["dtfmt"] = format_dt


async def current_admin(request: Request, session: AsyncSession) -> AdminUser | None:
    admin_id = request.session.get("admin_user_id")
    if not admin_id:
        return None
    admin = await session.get(AdminUser, admin_id)
    if admin is None or not admin.is_active:
        return None
    return admin


def render(request: Request, template: str, **context) -> HTMLResponse:
    context.setdefault("csrf_token", build_csrf_token(request))
    context.setdefault("request", request)
    return templates.TemplateResponse(template, context)


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return admin_redirect("/admin")


@router.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return render(request, "login.html", title="Вход")


@router.post("/admin/login")
async def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    verify_csrf(request, csrf_token)
    service = AdminAuthService(session, settings, remnawave)
    admin = await service.authenticate(username, password)
    if admin is None:
        return render(request, "login.html", title="Вход", error="Неверный логин или пароль.")
    request.session["admin_user_id"] = admin.id
    request.session["admin_username"] = admin.username
    return admin_redirect("/admin")


@router.post("/admin/logout")
async def logout(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
    verify_csrf(request, csrf_token)
    request.session.clear()
    return admin_redirect("/admin/login")


@router.get("/admin", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    data = await DashboardService(session, settings, remnawave).get_dashboard()
    return render(request, "dashboard.html", title="Dashboard", admin=admin, dashboard=data, page="dashboard")


@router.get("/admin/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    q: str | None = None,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    if q:
        users = await DashboardService(session, settings, remnawave).search_users(q)
    else:
        users = (await session.execute(select(User).order_by(desc(User.created_at)).limit(200))).scalars().all()
    return render(request, "users.html", title="Пользователи", admin=admin, users=users, page="users", query=q or "")


@router.get("/admin/users/{user_id}", response_class=HTMLResponse)
async def user_detail(
    request: Request,
    user_id: str,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    user = await session.get(User, user_id)
    if user is None:
        return admin_redirect("/admin/users")
    account_service = AccountService(session, settings, remnawave)
    traffic_service = TrafficService(session, settings, remnawave)
    summary = await account_service.get_profile_summary(user)
    usage = await traffic_service.get_usage_summary_for_user(user)
    topups = (
        await session.execute(
            select(TopupRequest).where(TopupRequest.user_id == user.id).order_by(desc(TopupRequest.created_at)).limit(20)
        )
    ).scalars().all()
    transactions = (
        await session.execute(
            select(BalanceTransaction)
            .where(BalanceTransaction.user_id == user.id)
            .order_by(desc(BalanceTransaction.created_at))
            .limit(20)
        )
    ).scalars().all()
    accesses = list(user.server_accesses)
    notifications = (
        await session.execute(
            select(Notification)
            .where(Notification.user_id == user.id)
            .order_by(desc(Notification.created_at))
            .limit(20)
        )
    ).scalars().all()
    plans = (await session.execute(select(Plan).where(Plan.is_trial.is_(False)).order_by(Plan.sort_order))).scalars().all()
    return render(
        request,
        "user_detail.html",
        title="Карточка пользователя",
        admin=admin,
        user=user,
        summary=summary,
        usage=usage,
        topups=topups,
        transactions=transactions,
        notifications=notifications,
        accesses=accesses,
        plans=plans,
        page="users",
    )


@router.post("/admin/users/{user_id}/balance")
async def user_balance_adjust(
    request: Request,
    user_id: str,
    amount_rub: str = Form(...),
    comment: str = Form(...),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    verify_csrf(request, csrf_token)
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    user = await session.get(User, user_id)
    if user:
        await BillingService(session, settings, remnawave).adjust_balance(
            user, admin, amount_rub=Decimal(amount_rub), comment=comment
        )
    return admin_redirect(f"/admin/users/{user_id}")


@router.post("/admin/users/{user_id}/trial")
async def user_give_trial(
    request: Request,
    user_id: str,
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    verify_csrf(request, csrf_token)
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    user = await session.get(User, user_id)
    if user:
        await SubscriptionService(session, settings, remnawave).activate_trial(user, admin_user_id=admin.id)
    return admin_redirect(f"/admin/users/{user_id}")


@router.post("/admin/users/{user_id}/activate-plan")
async def user_activate_plan(
    request: Request,
    user_id: str,
    plan_code: str = Form(...),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    verify_csrf(request, csrf_token)
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    user = await session.get(User, user_id)
    if user:
        await SubscriptionService(session, settings, remnawave).manual_set_active(user, plan_code)
    return admin_redirect(f"/admin/users/{user_id}")


@router.post("/admin/users/{user_id}/block")
async def user_block(
    request: Request,
    user_id: str,
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    verify_csrf(request, csrf_token)
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    user = await session.get(User, user_id)
    if user:
        await SubscriptionService(session, settings, remnawave).manual_deactivate(user)
    return admin_redirect(f"/admin/users/{user_id}")


@router.get("/admin/servers", response_class=HTMLResponse)
async def servers_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    servers = await ServerService(session, settings, remnawave).list_managed_servers()
    return render(request, "servers.html", title="Серверы", admin=admin, servers=servers, page="servers")


@router.post("/admin/servers/sync")
async def sync_servers_action(
    request: Request,
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    verify_csrf(request, csrf_token)
    if await current_admin(request, session) is None:
        return admin_redirect("/admin/login")
    await ServerService(session, settings, remnawave).sync_from_remnawave()
    return admin_redirect("/admin/servers")


@router.post("/admin/servers/{server_id}/toggle")
async def toggle_server(
    request: Request,
    server_id: str,
    is_enabled: str = Form(...),
    max_clients_count: int = Form(...),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    verify_csrf(request, csrf_token)
    if await current_admin(request, session) is None:
        return admin_redirect("/admin/login")
    await ServerService(session, settings, remnawave).set_server_management(
        server_id,
        is_enabled=is_enabled == "true",
        max_clients_count=max_clients_count,
    )
    return admin_redirect("/admin/servers")


@router.post("/admin/servers/{server_id}/remove")
async def remove_server(
    request: Request,
    server_id: str,
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    verify_csrf(request, csrf_token)
    if await current_admin(request, session) is None:
        return admin_redirect("/admin/login")
    await ServerService(session, settings, remnawave).remove_server_from_local_system(server_id)
    return admin_redirect("/admin/servers")


@router.get("/admin/topups", response_class=HTMLResponse)
async def topups_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    topups = (await session.execute(select(TopupRequest).order_by(desc(TopupRequest.created_at)))).scalars().all()
    return render(request, "topups.html", title="Заявки на пополнение", admin=admin, topups=topups, page="topups")


@router.post("/admin/topups/{topup_id}/approve")
async def approve_topup(
    request: Request,
    topup_id: str,
    comment: str = Form(default=""),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    verify_csrf(request, csrf_token)
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    topup = await session.get(TopupRequest, topup_id)
    if topup:
        await BillingService(session, settings, remnawave).approve_topup_request(topup, admin, comment=comment or None)
    return admin_redirect("/admin/topups")


@router.post("/admin/topups/{topup_id}/reject")
async def reject_topup(
    request: Request,
    topup_id: str,
    comment: str = Form(default=""),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    verify_csrf(request, csrf_token)
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    topup = await session.get(TopupRequest, topup_id)
    if topup:
        await BillingService(session, settings, remnawave).reject_topup_request(topup, admin, comment=comment or None)
    return admin_redirect("/admin/topups")


@router.get("/admin/plans", response_class=HTMLResponse)
async def plans_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    plans = (await session.execute(select(Plan).order_by(Plan.sort_order))).scalars().all()
    return render(request, "plans.html", title="Тарифы", admin=admin, plans=plans, page="plans")


@router.get("/admin/transactions", response_class=HTMLResponse)
async def transactions_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    transactions = (
        await session.execute(select(BalanceTransaction).order_by(desc(BalanceTransaction.created_at)).limit(200))
    ).scalars().all()
    return render(request, "transactions.html", title="Баланс и транзакции", admin=admin, transactions=transactions, page="transactions")


@router.get("/admin/traffic", response_class=HTMLResponse)
async def traffic_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    subscriptions = (
        await session.execute(select(Subscription).order_by(desc(Subscription.updated_at)).limit(200))
    ).scalars().all()
    return render(request, "traffic.html", title="Трафик и лимиты", admin=admin, subscriptions=subscriptions, page="traffic")


@router.get("/admin/online", response_class=HTMLResponse)
async def online_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    sessions = (
        await session.execute(select(OnlineSessionCache).order_by(desc(OnlineSessionCache.observed_at)).limit(200))
    ).scalars().all()
    return render(request, "online.html", title="Онлайн клиенты", admin=admin, sessions=sessions, page="online")


@router.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    settings_rows = (await session.execute(select(SystemSetting).order_by(SystemSetting.key.asc()))).scalars().all()
    return render(request, "settings.html", title="Настройки", admin=admin, settings_rows=settings_rows, page="settings")


@router.post("/admin/settings")
async def settings_update(
    request: Request,
    key: str = Form(...),
    value: str = Form(...),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    verify_csrf(request, csrf_token)
    if await current_admin(request, session) is None:
        return admin_redirect("/admin/login")
    row = await session.get(SystemSetting, key)
    if row is not None:
        row.value = value
    return admin_redirect("/admin/settings")


@router.get("/admin/events", response_class=HTMLResponse)
async def events_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = await current_admin(request, session)
    if admin is None:
        return admin_redirect("/admin/login")
    events = (await session.execute(select(SystemEvent).order_by(desc(SystemEvent.created_at)).limit(300))).scalars().all()
    return render(request, "events.html", title="Логи фоновых задач", admin=admin, events=events, page="events")
