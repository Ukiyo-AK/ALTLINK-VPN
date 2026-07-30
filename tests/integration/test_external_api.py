from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from altlink.application.services.base import AuthError
from altlink.domain.enums import PlanCode, UserStatus
from altlink.presentation.api.routes.external_api import router as external_api_router
from altlink.utils.time import utc_now


def external_api_test_app(test_services) -> FastAPI:
    app = FastAPI()
    app.state.container = test_services
    app.include_router(external_api_router)
    return app


@pytest.mark.asyncio
async def test_external_api_key_is_hashed_and_can_be_rotated(test_services):
    async with test_services.hub() as hub:
        issued = await hub.external_api.create_client(
            name="Partner service",
            description="Reads active ALTLINK members",
            scopes=["users.telegram_id", "users.status", "users.plan"],
            expires_at=None,
            admin_id=None,
        )
        assert issued.api_key.startswith(f"altlink_{issued.client.key_prefix}_")
        assert issued.client.key_hash != issued.api_key
        assert issued.api_key not in issued.client.key_hash

        authenticated = await hub.external_api.authenticate(
            issued.api_key,
            source_ip="127.0.0.1",
        )
        assert authenticated.id == issued.client.id
        assert authenticated.request_count == 1

        rotated = await hub.external_api.rotate_key(issued.client.id, admin_id=None)
        assert rotated.api_key != issued.api_key
        assert rotated.client.key_prefix != issued.client.key_prefix or rotated.api_key != issued.api_key

        with pytest.raises(AuthError, match="Неверный API-ключ"):
            await hub.external_api.authenticate(issued.api_key)
        assert (await hub.external_api.authenticate(rotated.api_key)).id == issued.client.id

        updated = await hub.external_api.update_scopes(
            issued.client.id,
            scopes=["users.status"],
            admin_id=None,
        )
        assert updated.scopes == ["users.status"]


@pytest.mark.asyncio
async def test_external_api_returns_only_active_users_and_allowed_fields(test_services):
    async with test_services.hub() as hub:
        active_user = await hub.accounts.get_or_create_user(
            telegram_id=41001,
            username="active_external",
            first_name="Active",
            last_name="External",
            language_code="ru",
        )
        inactive_user = await hub.accounts.get_or_create_user(
            telegram_id=41002,
            username="inactive_external",
            first_name="Inactive",
            last_name="External",
            language_code="ru",
        )
        await hub.topups.create_request(active_user.id, Decimal("250"), auto_complete=True)
        await hub.billing.activate_paid_plan(
            active_user.id,
            PlanCode.UNLIMITED,
            charge_user=True,
        )
        issued = await hub.external_api.create_client(
            name="Free companion service",
            description=None,
            scopes=["users.telegram_id", "users.status", "users.plan"],
            expires_at=None,
            admin_id=None,
        )

    app = external_api_test_app(test_services)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test.altlink",
    ) as client:
        response = await client.get(
            "/api/external/v1/users",
            headers={"X-API-Key": issued.api_key},
        )
        lookup_response = await client.get(
            f"/api/external/v1/users/by-telegram/{active_user.telegram_id}",
            headers={"X-API-Key": issued.api_key},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["active_only"] is True
    assert payload["meta"]["granted_fields"] == [
        "users.plan",
        "users.status",
        "users.telegram_id",
    ]
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["id"] == active_user.id
    assert item["telegram_id"] == active_user.telegram_id
    assert item["status"] == "active"
    assert item["access_active"] is True
    assert item["plan"]["code"] == PlanCode.UNLIMITED.value
    assert item["plan"]["name"] == "Pro • ежемесячно"
    assert inactive_user.id not in {record["id"] for record in payload["items"]}
    assert "balance_rub" not in item
    assert "profile" not in item
    assert "traffic" not in item
    assert lookup_response.status_code == 200
    assert lookup_response.json()["id"] == active_user.id
    assert lookup_response.json()["access_active"] is True


@pytest.mark.asyncio
async def test_external_api_does_not_grant_access_to_blocked_account(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=41004,
            username="blocked_external",
            first_name="Blocked",
            last_name="External",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("250"), auto_complete=True)
        await hub.billing.activate_paid_plan(
            user.id,
            PlanCode.UNLIMITED,
            charge_user=True,
        )
        user.status = UserStatus.BLOCKED
        issued = await hub.external_api.create_client(
            name="Access checker",
            description=None,
            scopes=["users.telegram_id", "users.status", "users.plan"],
            expires_at=None,
            admin_id=None,
        )

    app = external_api_test_app(test_services)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test.altlink",
    ) as client:
        list_response = await client.get(
            "/api/external/v1/users",
            headers={"X-API-Key": issued.api_key},
        )
        lookup_response = await client.get(
            f"/api/external/v1/users/by-telegram/{user.telegram_id}",
            headers={"X-API-Key": issued.api_key},
        )

    assert list_response.status_code == 200
    assert list_response.json()["items"] == []
    assert lookup_response.status_code == 200
    assert lookup_response.json()["status"] == "blocked"
    assert lookup_response.json()["access_active"] is False
    assert "plan" not in lookup_response.json()


@pytest.mark.asyncio
async def test_external_api_field_scope_prevents_telegram_and_plan_leaks(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=41003,
            username="status_only",
            first_name="Status",
            last_name="Only",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)
        issued = await hub.external_api.create_client(
            name="Status integration",
            description=None,
            scopes=["users.status"],
            expires_at=None,
            admin_id=None,
        )

    app = external_api_test_app(test_services)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test.altlink",
    ) as client:
        response = await client.get(
            "/api/external/v1/users",
            headers={"X-API-Key": issued.api_key},
        )
        lookup_response = await client.get(
            f"/api/external/v1/users/by-telegram/{user.telegram_id}",
            headers={"X-API-Key": issued.api_key},
        )

    assert response.status_code == 200
    assert lookup_response.status_code == 403
    item = response.json()["items"][0]
    assert item["status"] == "trial"
    assert item["access_active"] is True
    assert "telegram_id" not in item
    assert "plan" not in item
    assert "profile" not in item
    assert "balance_rub" not in item


@pytest.mark.asyncio
async def test_external_api_rejects_missing_disabled_and_expired_keys(test_services):
    async with test_services.hub() as hub:
        disabled = await hub.external_api.create_client(
            name="Disabled integration",
            description=None,
            scopes=["users.status"],
            expires_at=None,
            admin_id=None,
        )
        await hub.external_api.set_active(
            disabled.client.id,
            is_active=False,
            admin_id=None,
        )
        expired = await hub.external_api.create_client(
            name="Expiring integration",
            description=None,
            scopes=["users.status"],
            expires_at=utc_now() + timedelta(minutes=1),
            admin_id=None,
        )
        expired.client.expires_at = utc_now() - timedelta(seconds=1)

    app = external_api_test_app(test_services)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test.altlink",
    ) as client:
        missing_response = await client.get("/api/external/v1/users")
        disabled_response = await client.get(
            "/api/external/v1/users",
            headers={"X-API-Key": disabled.api_key},
        )
        expired_response = await client.get(
            "/api/external/v1/users",
            headers={"X-API-Key": expired.api_key},
        )

    assert missing_response.status_code == 401
    assert disabled_response.status_code == 401
    assert expired_response.status_code == 401


@pytest.mark.asyncio
async def test_external_api_openapi_declares_api_key_security(test_services):
    app = external_api_test_app(test_services)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test.altlink",
    ) as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert "/api/external/v1/users" in schema["paths"]
    security_scheme = schema["components"]["securitySchemes"]["ExternalApiKey"]
    assert security_scheme["type"] == "apiKey"
    assert security_scheme["in"] == "header"
    assert security_scheme["name"] == "X-API-Key"
