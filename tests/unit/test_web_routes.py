from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from altlink.domain.enums import PlanCode
from altlink.presentation.web.routes import (
    build_announce_header,
    group_portal_plans,
    is_foreign_latency_target,
    latency_probe,
    subscription_proxy,
    load_document_text,
    ensure_portal_login_attempt,
    portal_bot_login_url,
    portal_login_capabilities,
    portal_login_qr_data_url,
    portal_login_status,
    probe_server_latency,
    resolve_document_path,
    server_probe_port,
    LATENCY_RECHECK_THRESHOLD_MS,
)
from altlink.utils.subscriptions import build_client_announce_text, local_subscription_proxy_url


def plan(
    code: PlanCode,
    *,
    sort_order: int,
    price_rub: str,
    period_days: int,
    description: str,
    device_limit: int,
    is_trial: bool = False,
):
    return SimpleNamespace(
        code=code,
        sort_order=sort_order,
        price_rub=Decimal(price_rub),
        period_days=period_days,
        description=description,
        device_limit=device_limit,
        is_trial=is_trial,
    )


def test_group_portal_plans_merges_monthly_and_weekly_variants():
    groups = group_portal_plans(
        [
            plan(PlanCode.TRIAL, sort_order=0, price_rub="0", period_days=2, description="trial", device_limit=2, is_trial=True),
            plan(PlanCode.SINGLE_10GBIT, sort_order=10, price_rub="69", period_days=30, description="10g", device_limit=2),
            plan(PlanCode.SINGLE_10GBIT_WEEKLY, sort_order=15, price_rub="25", period_days=7, description="10g weekly", device_limit=2),
            plan(PlanCode.UNLIMITED, sort_order=20, price_rub="199", period_days=30, description="unlimited", device_limit=8),
            plan(PlanCode.UNLIMITED_WEEKLY, sort_order=25, price_rub="65", period_days=7, description="unlimited weekly", device_limit=8),
        ]
    )

    assert [group["family"] for group in groups] == ["10gbit", "unlimited"]
    assert groups[0]["title"] == "Start"
    assert groups[1]["title"] == "Pro"
    assert [period["plan_code"] for period in groups[0]["periods"]] == [
        PlanCode.SINGLE_10GBIT.value,
        PlanCode.SINGLE_10GBIT_WEEKLY.value,
    ]
    assert [period["plan_code"] for period in groups[1]["periods"]] == [
        PlanCode.UNLIMITED.value,
        PlanCode.UNLIMITED_WEEKLY.value,
    ]


def test_group_portal_plans_keeps_device_limit_on_group():
    groups = group_portal_plans(
        [plan(PlanCode.UNLIMITED, sort_order=20, price_rub="199", period_days=30, description="unlimited", device_limit=8)]
    )

    assert groups == [
        {
            "family": "unlimited",
            "title": "Pro",
            "description": "unlimited",
            "device_limit": 8,
            "periods": [
                {
                    "label": "На месяц",
                    "caption": "Основной формат",
                    "price_rub": Decimal("199"),
                    "plan_code": PlanCode.UNLIMITED.value,
                    "period_days": 30,
                }
            ],
        }
    ]


def test_resolve_document_path_falls_back_to_matching_markdown_name(monkeypatch, tmp_path: Path):
    docs = tmp_path / "document"
    docs.mkdir()
    custom = docs / "altlink_privacy_policy_custom.md"
    custom.write_text("# Privacy", encoding="utf-8")

    monkeypatch.setattr("altlink.presentation.web.routes.document_root", lambda: docs)

    assert resolve_document_path("privacy") == custom


def test_load_document_text_supports_utf8_bom(monkeypatch, tmp_path: Path):
    docs = tmp_path / "document"
    docs.mkdir()
    agreement = docs / "altlink_user_agreement.md"
    agreement.write_text("\ufeff# Agreement", encoding="utf-8")

    monkeypatch.setattr("altlink.presentation.web.routes.document_root", lambda: docs)

    assert load_document_text("agreement") == "# Agreement"


def test_server_latency_helpers_filter_and_choose_port():
    ru_server = SimpleNamespace(country_code="RU", inbounds=[SimpleNamespace(port=8443, is_active=True)])
    nl_server = SimpleNamespace(country_code="NL", inbounds=[SimpleNamespace(port=2053, is_active=True)])
    no_inbounds = SimpleNamespace(country_code="DE", inbounds=[])

    assert is_foreign_latency_target(ru_server) is False
    assert is_foreign_latency_target(nl_server) is True
    assert server_probe_port(nl_server) == 2053
    assert server_probe_port(no_inbounds) == 443


@pytest.mark.asyncio
async def test_probe_server_latency_rechecks_high_values(monkeypatch):
    server = SimpleNamespace(name="FI Node", country_code="FI", address="node.example", inbounds=[])
    calls = [
        {"name": server.name, "country_code": "FI", "latency_ms": LATENCY_RECHECK_THRESHOLD_MS + 120, "reachable": True},
        {"name": server.name, "country_code": "FI", "latency_ms": 148, "reachable": True},
    ]

    async def fake_single_probe(target, *, timeout_seconds: float = 2.5):
        assert target is server
        return calls.pop(0)

    monkeypatch.setattr("altlink.presentation.web.routes.single_probe_server_latency", fake_single_probe)

    result = await probe_server_latency(server)

    assert result["reachable"] is True
    assert result["rechecked"] is True
    assert result["attempts"] == 2
    assert result["initial_latency_ms"] == LATENCY_RECHECK_THRESHOLD_MS + 120
    assert result["second_latency_ms"] == 148
    assert result["latency_ms"] == 148


@pytest.mark.asyncio
async def test_latency_probe_disables_cache_and_returns_sorted_probes(monkeypatch):
    servers = [
        SimpleNamespace(id="ru-1", name="RU Main", address="ru.example.com", country_code="RU", is_available=True, is_connected=True, inbounds=[]),
        SimpleNamespace(id="nl-1", name="NL Node", address="nl.example.com", country_code="NL", is_available=True, is_connected=True, inbounds=[]),
        SimpleNamespace(id="de-1", name="DE Node", address="de.example.com", country_code="DE", is_available=True, is_connected=True, inbounds=[]),
    ]

    async def fake_list_servers():
        return servers

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(catalog=SimpleNamespace(list_servers=fake_list_servers))

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(hub=fake_hub),
                settings=SimpleNamespace(
                    latency_probe_scheme="https",
                    latency_probe_port=44443,
                    latency_probe_path="/ping",
                    browser_latency_timeout_ms=4000,
                ),
            )
        )
    )

    response = await latency_probe(request)

    assert response.status_code == 200
    assert response.headers["Cache-Control"].startswith("no-store")
    assert b"browser_rtt" in response.body
    assert b"timeout_ms" in response.body
    assert b"recheck_threshold_ms" in response.body
    assert b"disclaimer" in response.body
    assert b"NL Node" in response.body
    assert b"server_id\":\"nl-1" in response.body
    assert b"probe_url\":\"https://nl.example.com:44443/ping" in response.body
    assert b"RU Main" not in response.body


@pytest.mark.asyncio
async def test_latency_probe_can_limit_to_requested_servers_and_include_local(monkeypatch):
    servers = [
        SimpleNamespace(id="ru-1", name="RU Main", address="ru.example.com", country_code="RU", is_available=True, is_connected=True, inbounds=[]),
        SimpleNamespace(id="nl-1", name="NL Node", address="nl.example.com", country_code="NL", is_available=True, is_connected=True, inbounds=[]),
        SimpleNamespace(id="de-1", name="DE Node", address="de.example.com", country_code="DE", is_available=True, is_connected=False, inbounds=[]),
    ]

    async def fake_list_servers():
        return servers

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(catalog=SimpleNamespace(list_servers=fake_list_servers))

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(hub=fake_hub),
                settings=SimpleNamespace(
                    latency_probe_scheme="https",
                    latency_probe_port=44443,
                    latency_probe_path="/ping",
                    browser_latency_timeout_ms=3500,
                ),
            )
        ),
        query_params={"server_ids": "ru-1,de-1", "include_local": "1"},
    )

    response = await latency_probe(request)

    assert response.status_code == 200
    assert b"RU Main" in response.body
    assert b"server_id\":\"ru-1" in response.body
    assert b"probe_url\":\"https://ru.example.com:44443/ping" in response.body
    assert b"3500" in response.body
    assert b"DE Node" not in response.body
    assert b"NL Node" not in response.body


def test_portal_login_capabilities_require_valid_bot_configuration():
    settings = SimpleNamespace(
        client_bot_token="",
        client_bot_name="@altlink_bot",
        backend_public_url="https://altlink.online",
        debug=False,
    )
    enabled, issue, dev_login_enabled = portal_login_capabilities(settings)

    assert enabled is False
    assert "CLIENT_BOT_TOKEN" in str(issue)
    assert dev_login_enabled is False


def test_portal_bot_login_helpers_build_deeplink_and_qr():
    settings = SimpleNamespace(client_bot_name="@altlink_bot")

    deep_link = portal_bot_login_url(settings, "demo-token")
    qr_data_url = portal_login_qr_data_url(deep_link)

    assert deep_link == "https://t.me/altlink_bot?start=login_demo-token"
    assert qr_data_url is not None
    assert qr_data_url.startswith("data:image/png;base64,")


def test_local_subscription_proxy_url_builds_backend_link():
    settings = SimpleNamespace(backend_public_url="https://altlink.online")

    assert local_subscription_proxy_url(settings, "demo-short") == "https://altlink.online/sub/demo-short"
    assert local_subscription_proxy_url(settings, "demo-short", "clash") == "https://altlink.online/sub/demo-short/clash"


def test_build_client_announce_text_includes_tariff_balance_and_status():
    settings = SimpleNamespace()
    user = SimpleNamespace(balance_rub=Decimal("149.50"), status="active")
    subscription = SimpleNamespace(plan=SimpleNamespace(name="Pro", device_limit=8))

    text = build_client_announce_text(user, subscription, settings)

    assert "🧾 Тариф: Pro" in text
    assert "💳 Баланс: 149.50 ₽" in text
    assert "📱 Устройств: до 8" in text
    assert "🔔 Статус: доступ активен" in text


def test_build_announce_header_appends_upstream_announce():
    settings = SimpleNamespace()
    user = SimpleNamespace(balance_rub=Decimal("75"), status="trial")
    subscription = SimpleNamespace(plan=SimpleNamespace(name="Тестовый период", device_limit=2))

    header = build_announce_header(user, subscription, settings, "base64:0J/RgNC40LLQtdGC")

    assert header.startswith("base64:")
    assert "Привет" in base64.b64decode(header.split(":", 1)[1]).decode("utf-8")


@pytest.mark.asyncio
async def test_subscription_proxy_overrides_announce_and_preserves_upstream_body(test_services, monkeypatch):
    test_services.settings.backend_public_url = "https://altlink.online"
    test_services.settings.remnawave_subscription_base_url = "https://sub.remna.example"

    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=42001,
            username="announce_user",
            first_name="Announce",
            last_name="User",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)
        user = await hub.accounts.get_user(user.id)
        short_uuid = user.remnawave_short_uuid

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, params=None, headers=None):
            assert url == f"https://sub.remna.example/{short_uuid}"
            assert headers["user-agent"] == "Happ/1.0"
            assert headers["x-hwid"] == "device-1"
            return SimpleNamespace(
                status_code=200,
                content=b"vmess://demo",
                headers={
                    "content-type": "text/plain; charset=utf-8",
                    "content-disposition": "attachment; filename=demo",
                    "announce": "base64:0J/RgNC40LLQtdGC",
                },
            )

    monkeypatch.setattr("altlink.presentation.web.routes.httpx.AsyncClient", FakeAsyncClient)

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(container=test_services, settings=test_services.settings)),
        headers={
            "user-agent": "Happ/1.0",
            "x-hwid": "device-1",
        },
        query_params=SimpleNamespace(multi_items=lambda: []),
    )

    response = await subscription_proxy(request, short_uuid)

    assert response.status_code == 200
    assert response.body == b"vmess://demo"
    assert response.headers["content-disposition"] == "attachment; filename=demo"
    decoded = base64.b64decode(response.headers["announce"].split(":", 1)[1]).decode("utf-8")
    assert "🧾 Тариф:" in decoded
    assert "💳 Баланс:" in decoded
    assert "Привет" in decoded


@pytest.mark.asyncio
async def test_portal_login_status_returns_missing_without_attempt_token(test_services):
    request = SimpleNamespace(
        session={},
        app=SimpleNamespace(state=SimpleNamespace(container=test_services)),
    )

    response = await portal_login_status(request)

    assert response.status_code == 200
    assert b"missing" in response.body


@pytest.mark.asyncio
async def test_ensure_portal_login_attempt_uses_token_from_query(test_services):
    async with test_services.hub() as hub:
        attempt = await hub.portal_auth.create_login_attempt()

        request = SimpleNamespace(
            session={},
            query_params={"token": attempt.token},
        )

        resolved = await ensure_portal_login_attempt(request, hub)

    assert resolved is not None
    assert resolved.token == attempt.token
    assert request.session["portal_login_attempt_token"] == attempt.token


@pytest.mark.asyncio
async def test_portal_login_status_consumes_approved_attempt_and_sets_session(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=41001,
            username="portal_status",
            first_name="Portal",
            last_name="Status",
            language_code="ru",
        )
        attempt = await hub.portal_auth.create_login_attempt()
        await hub.portal_auth.approve_login_attempt(attempt.token, user.id)

    request = SimpleNamespace(
        session={"portal_login_attempt_token": attempt.token},
        app=SimpleNamespace(state=SimpleNamespace(container=test_services)),
    )

    response = await portal_login_status(request)

    assert response.status_code == 200
    assert b"approved" in response.body
    assert request.session["portal_user_id"] == user.id
    assert "portal_login_attempt_token" not in request.session
