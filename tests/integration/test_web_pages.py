import base64
import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware

from altlink.domain.enums import PlanCode, PromoRewardKind
from altlink.infrastructure.db.models import AdminUser
from altlink.presentation.web import routes
from altlink.utils.time import utc_now


def web_test_app(services):
    app = FastAPI()
    app.state.settings = services.settings
    app.state.container = services
    app.add_middleware(SessionMiddleware, secret_key=services.settings.session_secret_key)
    app.include_router(routes.router)
    return app


def session_cookie(services, **data):
    raw = base64.b64encode(json.dumps({"csrf_token": "audit-token", **data}).encode())
    return TimestampSigner(services.settings.session_secret_key).sign(raw).decode()


async def seed_web_user(services, scenario):
    services.settings.required_subscription_channel = ""
    async with services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=770100, username="audit_user", first_name="A very long account name " * 5,
            last_name="<script>alert(1)</script>", language_code="ru",
        )
        user_id = user.id
        if scenario in {"trial", "expired"}:
            subscription = await hub.billing.activate_trial(user_id)
            if scenario == "expired":
                subscription.ends_at = utc_now() - timedelta(hours=1)
                subscription.next_billing_at = subscription.ends_at
                await hub.billing.sync_user_trial_state(user_id)
        elif scenario in {"start", "pro"}:
            user.balance_rub = Decimal("500")
            await hub.billing.activate_paid_plan(user_id, PlanCode.SINGLE_10GBIT if scenario == "start" else PlanCode.UNLIMITED)
        await hub.support.create_request(user_id=user_id, message="A test support message")
        await hub.topups.create_request(user_id, Decimal("100"), provider_code="manual", auto_complete=False)
    return user_id


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["new", "trial", "expired", "start", "pro"])
async def test_portal_renders_real_account_states(test_services, scenario):
    user_id = await seed_web_user(test_services, scenario)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(web_test_app(test_services)), base_url="https://altlink.online") as client:
        client.cookies.set("session", session_cookie(test_services, portal_user_id=user_id))
        response = await client.get("/portal")
    assert response.status_code == 200
    assert "Личный кабинет" in response.text
    assert "автопродлен" not in response.text.casefold()
    assert 'data-portal-page="subscription"' in response.text
    assert "A test support message" in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/admin/dashboard", "/admin/analytics", "/admin/users", "/admin/servers",
    "/admin/plans", "/admin/topups", "/admin/transactions", "/admin/traffic",
    "/admin/online", "/admin/promos", "/admin/settings", "/admin/api-clients",
    "/admin/api-docs", "/admin/events", "/admin/support", "/admin/users/{user_id}",
])
async def test_admin_pages_render_with_real_records(test_services, path):
    user_id = await seed_web_user(test_services, "start")
    async with test_services.hub() as hub:
        admin = AdminUser(username="audit_admin", password_hash="not-used-for-cookie-tests", is_active=True)
        hub.session.add(admin)
        await hub.session.flush()
        admin_id = admin.id
        if path == "/admin/traffic":
            subscription = await hub.accounts.get_current_subscription(user_id)
            subscription.whitelist_traffic_used_bytes = 123456789
    async with httpx.AsyncClient(transport=httpx.ASGITransport(web_test_app(test_services)), base_url="https://altlink.online") as client:
        client.cookies.set("session", session_cookie(test_services, admin_id=admin_id))
        response = await client.get(path.format(user_id=user_id))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    if path == "/admin/traffic":
        assert "<td>0.23 \u20bd</td>" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/portal/login", "/help/connect", "/legal/agreement", "/legal/privacy", "/admin/login"])
async def test_public_pages_render(test_services, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(web_test_app(test_services)), base_url="https://altlink.online") as client:
        response = await client.get(path)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_disabled_admin_cannot_reuse_existing_session(test_services):
    async with test_services.hub() as hub:
        admin = AdminUser(username="disabled_admin", password_hash="unused", is_active=False)
        hub.session.add(admin)
        await hub.session.flush()
        admin_id = admin.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(web_test_app(test_services)), base_url="https://altlink.online") as client:
        client.cookies.set("session", session_cookie(test_services, admin_id=admin_id))
        response = await client.get("/admin/dashboard")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/auth/telegram-webapp", "/api/payments/create", "/api/promo/apply", "/api/whitelist/purchases"])
@pytest.mark.parametrize("body", [[], "not-an-object", None])
async def test_api_rejects_non_object_json_without_server_error(test_services, path, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(web_test_app(test_services)), base_url="https://altlink.online") as client:
        response = await client.post(path, content=json.dumps(body), headers={"content-type": "application/json"})
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["traffic_min", "balance_min", "whitelist_traffic_min"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999999", "1000000000000000000"])
async def test_user_filters_tolerate_nonfinite_numbers(test_services, field, value):
    async with test_services.hub() as hub:
        admin = AdminUser(username="filter_admin", password_hash="unused")
        hub.session.add(admin)
        await hub.session.flush()
        admin_id = admin.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(web_test_app(test_services)), base_url="https://altlink.online") as client:
        client.cookies.set("session", session_cookie(test_services, admin_id=admin_id))
        response = await client.get("/admin/users", params={field: value})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_portal_shows_pending_discount_and_consumes_it_only_on_activation(test_services):
    user_id = await seed_web_user(test_services, "new")
    async with test_services.hub() as hub:
        await hub.promos.create_code(
            code="WEB10", name="Web discount", reward_kind=PromoRewardKind.PLAN_DISCOUNT,
            reward_value=Decimal("10"), usage_limit=1, expires_at=None,
            new_users_only=False, admin_id=None,
        )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(web_test_app(test_services)), base_url="https://altlink.online") as client:
        client.cookies.set("session", session_cookie(test_services, portal_user_id=user_id))
        applied = await client.post("/api/promo/apply", json={"csrf_token": "audit-token", "code": "WEB10"})
        assert applied.status_code == 200
        assert applied.json()["plan_prices"][PlanCode.UNLIMITED.value]["price"] == "179.1"
        assert applied.json()["balance"] == "0"
        response = await client.get("/portal")
        assert response.status_code == 200
        assert "<s>199 \u20bd</s>" in response.text
        assert 'class="plan-period-price">179.1 \u20bd' in response.text
        async with test_services.hub() as hub:
            user = await hub.accounts.get_user(user_id)
            user.balance_rub = Decimal("500")
            await hub.billing.activate_paid_plan(user_id, PlanCode.UNLIMITED)
            assert user.balance_rub == Decimal("320.90")
        response = await client.get("/portal")
        assert 'data-plan-discount hidden><s>199 \u20bd</s>' in response.text
        assert 'class="plan-period-price">199 \u20bd' in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/admin/logout", "/portal/logout"])
async def test_logout_requires_csrf(test_services, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(web_test_app(test_services)), base_url="https://altlink.online") as client:
        client.cookies.set("session", session_cookie(test_services))
        assert (await client.post(path)).status_code == 400
        assert (await client.post(path, data={"csrf_token": "audit-token"})).status_code == 303


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme,secure", [("https", True), ("http", False)])
async def test_real_app_session_cookie_security(test_services, monkeypatch, scheme, secure):
    from altlink.presentation.web import app as app_module

    test_services.settings.backend_public_url = f"{scheme}://altlink.example"
    monkeypatch.setattr(app_module, "get_settings", lambda: test_services.settings)
    app = app_module.create_app()
    app.state.settings = test_services.settings
    app.state.container = test_services
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=f"{scheme}://altlink.example") as client:
        response = await client.get("/admin/login")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert ("secure" in cookie) is secure
