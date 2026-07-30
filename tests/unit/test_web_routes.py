from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from altlink.presentation.web import routes as web_routes
from altlink.domain.enums import PlanCode
from altlink.utils.latency import single_probe_server_latency
from altlink.presentation.web.routes import (
    group_portal_plans,
    is_foreign_latency_target,
    latency_probe,
    load_document_text,
    ensure_portal_login_attempt,
    build_portal_context,
    portal_bot_login_url,
    portal_login_capabilities,
    portal_login_qr_data_url,
    portal_login_status,
    probe_server_latency,
    resolve_document_path,
    server_probe_port,
    LATENCY_RECHECK_THRESHOLD_MS,
)
from altlink.utils.latency import (
    LEGACY_WHITELIST_LATENCY_TARGET_SETTING_KEY,
    WHITELIST_SERVER_DOMAIN_SETTING_KEY,
)


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
    assert [period["price_label"] for period in groups[0]["periods"]] == ["69", "25"]
    assert [period["price_label"] for period in groups[1]["periods"]] == ["199", "65"]


def test_parse_date_query_uses_moscow_day_boundaries():
    assert web_routes.parse_date_query("2026-07-08") == datetime(2026, 7, 7, 21, 0, tzinfo=UTC)
    assert web_routes.parse_date_query("2026-07-08", end_of_day=True) == datetime(
        2026,
        7,
        8,
        20,
        59,
        59,
        999999,
        tzinfo=UTC,
    )


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
                    "price_label": "199",
                    "plan_code": PlanCode.UNLIMITED.value,
                    "period_days": 30,
                }
            ],
        }
    ]


def test_latency_quality_label_uses_positive_landing_scale():
    assert web_routes.latency_quality_label(None) == "Проверьте пинг"
    assert web_routes.latency_quality_label(7) == "Лучший отклик"
    assert web_routes.latency_quality_label(30) == "Лучший отклик"
    assert web_routes.latency_quality_label(40) == "Быстрое соединение"
    assert web_routes.latency_quality_label(71) == "Стабильное соединение"
    assert web_routes.latency_quality_label(121) == "Подходит для повседневных задач"
    assert web_routes.latency_quality_label(250) == "Дальняя локация"
    assert web_routes.latency_quality_label(301) == "Временно высокая задержка"


def test_portal_user_facing_status_labels_are_translated():
    assert web_routes.payment_status_label("approved") == "Зачислено"
    assert web_routes.payment_status_label("new") == "Новый"
    assert web_routes.payment_status_label("Зачислено") == "Зачислено"
    assert web_routes.payment_status_label("Отклонён") == "Отклонён"
    assert web_routes.payment_status_label("Отменён") == "Отменён"
    assert web_routes.payment_status_label("Новый") == "Новый"
    assert web_routes.payment_status_label("Оплачен") == "Оплачен"
    assert web_routes.payment_status_label("Ожидает оплаты") == "Ожидает оплаты"
    assert web_routes.payment_status_label("Истёк") == "Истёк"
    assert web_routes.payment_status_label("succeeded") == "Оплачен"
    assert web_routes.payment_status_label("paid") == "Оплачен"
    assert web_routes.payment_status_label("pending") == "Ожидает оплаты"
    assert web_routes.payment_status_label("waiting_for_capture") == "Ожидает подтверждения"
    assert web_routes.payment_status_label("canceled") == "Отменён"
    assert web_routes.payment_status_label("cancelled") == "Отменён"
    assert web_routes.payment_status_label("rejected") == "Отклонён"
    assert web_routes.payment_status_label("expired") == "Истёк"
    assert web_routes.payment_status_label("unknown") == "Неизвестный статус"
    assert web_routes.access_status_label("active") == "Активен"
    assert web_routes.access_status_label("inactive") == "Неактивен"
    assert web_routes.access_status_label("disabled") == "Отключён"
    assert web_routes.access_status_label("maintenance") == "Обслуживание"
    assert web_routes.access_status_label("unknown") == "Статус неизвестен"


def test_render_injects_asset_version_and_disables_html_cache(monkeypatch):
    captured: dict[str, object] = {}
    response = SimpleNamespace(headers={})

    class DummyTemplates:
        def TemplateResponse(self, *, request, name: str, context: dict):
            captured["request"] = request
            captured["template_name"] = name
            captured["context"] = context
            return response

    request = SimpleNamespace(session={})
    monkeypatch.setattr(web_routes, "templates", DummyTemplates())

    result = web_routes.render(request, "landing.html", title="ALTLINK")

    assert result is response
    assert captured["template_name"] == "landing.html"
    assert captured["context"]["asset_version"] == web_routes.ASSET_VERSION
    assert captured["context"]["title"] == "ALTLINK"
    assert response.headers["Cache-Control"] == "no-cache, no-store, must-revalidate"
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["Expires"] == "0"


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
async def test_single_probe_server_latency_uses_normalized_host(monkeypatch):
    captured: dict[str, object] = {}
    server = SimpleNamespace(
        name="NL Node",
        country_code="NL",
        address="https://nl.example.com:9443/node",
        inbounds=[SimpleNamespace(port=2053, is_active=True)],
    )

    class DummyWriter:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def fake_open_connection(host, port):
        captured["host"] = host
        captured["port"] = port
        return object(), DummyWriter()

    monkeypatch.setattr("asyncio.open_connection", fake_open_connection)

    result = await single_probe_server_latency(server)

    assert result["reachable"] is True
    assert captured == {"host": "nl.example.com", "port": 2053}


@pytest.mark.asyncio
async def test_single_probe_server_latency_can_use_manual_whitelist_server_domain(monkeypatch):
    captured: dict[str, object] = {}
    server = SimpleNamespace(
        name="Whitelist Node",
        country_code="NL",
        address="https://node.example.com:9443/node",
        inbounds=[SimpleNamespace(port=2053, is_active=True)],
    )

    class DummyWriter:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def fake_open_connection(host, port):
        captured["host"] = host
        captured["port"] = port
        return object(), DummyWriter()

    monkeypatch.setattr("asyncio.open_connection", fake_open_connection)

    result = await single_probe_server_latency(
        server,
        override_host="wl.altlink.online",
    )

    assert result["reachable"] is True
    assert result["probe_target_host"] == "wl.altlink.online"
    assert result["probe_target_port"] == 2053
    assert captured == {"host": "wl.altlink.online", "port": 2053}


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
                    admin_bot_token="",
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
                    admin_bot_token="",
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
    assert b"DE Node" in response.body
    assert b"server_id\":\"de-1" in response.body
    assert b"probe_url\":\"https://de.example.com:44443/ping" in response.body
    assert b"is_connected\":false" in response.body
    assert b"3500" in response.body
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


@pytest.mark.asyncio
async def test_admin_dashboard_route_passes_chart_context_and_refreshes_on_demand(monkeypatch):
    sync_calls: list[str] = []
    rendered: dict[str, object] = {}
    overview = {
        "active_users": 1,
        "renewal_disabled_users": 0,
        "blocked_users": 0,
        "trial_users": 0,
        "payments_total_rub": Decimal("100"),
        "total_traffic_bytes": 5 * 1024**3,
        "period": "1w",
        "charts": {
            "plan_mix": {"labels": ["Start", "Pro"], "values": [1, 2]},
            "user_statuses": {"labels": [], "values": []},
            "server_loads": {"labels": [], "values": [], "types": []},
            "payments": {"labels": [], "values": []},
            "server_types": {"labels": [], "values": []},
        },
        "top_users": [],
        "recent_topups": [],
    }

    async def fake_snapshot_traffic():
        sync_calls.append("traffic")

    async def fake_sync_servers():
        sync_calls.append("servers")

    async def fake_overview(period="2w"):
        sync_calls.append(f"overview:{period}")
        return overview

    async def fake_resolve_admin(request, hub):
        return SimpleNamespace(id="admin-1", username="admin")

    def fake_render(request, template_name: str, **context):
        rendered["template_name"] = template_name
        rendered["context"] = context
        return context

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            billing=SimpleNamespace(snapshot_traffic=fake_snapshot_traffic),
            catalog=SimpleNamespace(sync_servers=fake_sync_servers),
            dashboard=SimpleNamespace(overview=fake_overview),
        )

    request = SimpleNamespace(
        session={"admin_id": "admin-1"},
        app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(hub=fake_hub))),
    )

    monkeypatch.setattr(web_routes, "resolve_admin", fake_resolve_admin)
    monkeypatch.setattr(web_routes, "render", fake_render)

    response = await web_routes.dashboard(request, period="1w", refresh=True)

    assert response is rendered["context"]
    assert sync_calls == ["servers", "traffic", "overview:1w"]
    assert rendered["template_name"] == "dashboard.html"
    assert rendered["context"]["charts"] == overview["charts"]
    assert rendered["context"]["charts_json"]
    assert rendered["context"]["selected_period"] == "1w"
    assert rendered["context"]["refresh_requested"] is True


@pytest.mark.asyncio
async def test_admin_dashboard_route_uses_cached_data_without_refresh(monkeypatch):
    sync_calls: list[str] = []
    rendered: dict[str, object] = {}
    overview = {
        "period": "2w",
        "charts": {"plan_mix": {"labels": ["Start", "Pro"], "values": [0, 0]}},
    }

    async def fake_snapshot_traffic():
        sync_calls.append("traffic")

    async def fake_sync_servers():
        sync_calls.append("servers")

    async def fake_overview(period="2w"):
        sync_calls.append(f"overview:{period}")
        return overview

    async def fake_resolve_admin(request, hub):
        return SimpleNamespace(id="admin-1", username="admin")

    def fake_render(request, template_name: str, **context):
        rendered["template_name"] = template_name
        rendered["context"] = context
        return context

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            billing=SimpleNamespace(snapshot_traffic=fake_snapshot_traffic),
            catalog=SimpleNamespace(sync_servers=fake_sync_servers),
            dashboard=SimpleNamespace(overview=fake_overview),
        )

    request = SimpleNamespace(
        session={"admin_id": "admin-1"},
        app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(hub=fake_hub))),
    )

    monkeypatch.setattr(web_routes, "resolve_admin", fake_resolve_admin)
    monkeypatch.setattr(web_routes, "render", fake_render)

    response = await web_routes.dashboard(request, period="2w", refresh=False)

    assert response is rendered["context"]
    assert sync_calls == ["overview:2w"]
    assert rendered["template_name"] == "dashboard.html"
    assert rendered["context"]["refresh_requested"] is False


@pytest.mark.asyncio
async def test_admin_traffic_route_uses_dashboard_rows(monkeypatch):
    rendered: dict[str, object] = {}
    rows = [
        SimpleNamespace(
            user=SimpleNamespace(username="leader", telegram_id=101),
            plan=SimpleNamespace(name="Start"),
            traffic_used_bytes=9 * 1024**3,
            whitelist_traffic_used_bytes=2 * 1024**3,
            auto_renew=True,
        )
    ]

    async def fake_resolve_admin(request, hub):
        return SimpleNamespace(id="admin-1", username="admin")

    async def fake_snapshot_traffic():
        return None

    async def fake_list_traffic_rows():
        return rows

    def fake_render(request, template_name: str, **context):
        rendered["template_name"] = template_name
        rendered["context"] = context
        return context

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            billing=SimpleNamespace(snapshot_traffic=fake_snapshot_traffic),
            dashboard=SimpleNamespace(list_traffic_rows=fake_list_traffic_rows),
        )

    request = SimpleNamespace(
        session={"admin_id": "admin-1"},
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(hub=fake_hub),
                settings=SimpleNamespace(whitelist_price_per_gb_rub=Decimal("15")),
            )
        ),
    )

    monkeypatch.setattr(web_routes, "resolve_admin", fake_resolve_admin)
    monkeypatch.setattr(web_routes, "render", fake_render)

    response = await web_routes.traffic_page(request)

    assert response is rendered["context"]
    assert rendered["template_name"] == "traffic.html"
    assert rendered["context"]["subscriptions"] == rows


@pytest.mark.asyncio
async def test_admin_servers_route_passes_latency_state(monkeypatch):
    rendered: dict[str, object] = {}
    servers = [SimpleNamespace(id="server-1", name="Whitelist NL")]
    sync_servers = AsyncMock()

    async def fake_resolve_admin(request, hub):
        return SimpleNamespace(id="admin-1", username="admin")

    def fake_render(request, template_name: str, **context):
        rendered["template_name"] = template_name
        rendered["context"] = context
        return context

    async def fake_scalar(_query):
        return SimpleNamespace(
            value={
                "checked_at": "2026-05-17T12:00:00+00:00",
                "servers": {
                    "server-1": {
                        "reachable": True,
                        "latency_ms": 87,
                        "probe_target_host": "wl.altlink.online",
                        "probe_target_port": 44443,
                        "checked_at": "2026-05-17T12:00:00+00:00",
                    }
                },
            }
        )

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            catalog=SimpleNamespace(sync_servers=sync_servers, list_servers=AsyncMock(return_value=servers)),
            session=SimpleNamespace(scalar=fake_scalar),
        )

    request = SimpleNamespace(
        session={"admin_id": "admin-1"},
        app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(hub=fake_hub))),
    )

    monkeypatch.setattr(web_routes, "resolve_admin", fake_resolve_admin)
    monkeypatch.setattr(web_routes, "render", fake_render)

    response = await web_routes.servers_page(request)

    assert response is rendered["context"]
    assert rendered["template_name"] == "servers.html"
    assert rendered["context"]["server_latency_checked_at"] == "2026-05-17T12:00:00+00:00"
    assert rendered["context"]["server_latency_state"]["server-1"]["latency_ms"] == 87
    assert rendered["context"]["server_latency_state"]["server-1"]["probe_target_host"] == "wl.altlink.online"
    sync_servers.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_server_catalog_if_possible_rolls_back_after_failure():
    async def broken_sync_servers():
        raise RuntimeError("panel unavailable")

    rollback = AsyncMock()
    hub = SimpleNamespace(
        catalog=SimpleNamespace(sync_servers=broken_sync_servers),
        session=SimpleNamespace(rollback=rollback),
    )

    await web_routes.sync_server_catalog_if_possible(hub)

    rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_servers_route_renders_after_sync_failure(monkeypatch):
    rendered: dict[str, object] = {}
    servers = [SimpleNamespace(id="server-1", name="Whitelist NL")]
    sync_servers = AsyncMock(side_effect=RuntimeError("panel unavailable"))

    async def fake_resolve_admin(request, hub):
        return SimpleNamespace(id="admin-1", username="admin")

    def fake_render(request, template_name: str, **context):
        rendered["template_name"] = template_name
        rendered["context"] = context
        return context

    async def fake_scalar(_query):
        return None

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            catalog=SimpleNamespace(sync_servers=sync_servers, list_servers=AsyncMock(return_value=servers)),
            session=SimpleNamespace(scalar=fake_scalar, rollback=AsyncMock()),
        )

    request = SimpleNamespace(
        session={"admin_id": "admin-1"},
        app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(hub=fake_hub))),
    )

    monkeypatch.setattr(web_routes, "resolve_admin", fake_resolve_admin)
    monkeypatch.setattr(web_routes, "render", fake_render)

    response = await web_routes.servers_page(request)

    assert response is rendered["context"]
    assert rendered["template_name"] == "servers.html"
    assert rendered["context"]["servers"] == servers
    sync_servers.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_users_sync_access_route_sets_flash(monkeypatch):
    sync_users = AsyncMock(
        return_value={
            "total": 2,
            "synced": 2,
            "created": 1,
            "updated": 1,
            "recreated": 0,
            "empty_squads": 0,
            "failed": 0,
            "catalog_synced": True,
        }
    )

    async def fake_resolve_admin(request, hub):
        return SimpleNamespace(id="admin-1", username="admin")

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(billing=SimpleNamespace(sync_users_with_available_nodes=sync_users))

    class DummyRequest(SimpleNamespace):
        async def form(self):
            return {"csrf_token": "token"}

    request = DummyRequest(
        session={"admin_id": "admin-1", "csrf_token": "token"},
        app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(hub=fake_hub))),
    )

    monkeypatch.setattr(web_routes, "resolve_admin", fake_resolve_admin)

    response = await web_routes.users_sync_access(request)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/users"
    assert request.session["flash"]["level"] == "success"
    assert "обновлено 2" in request.session["flash"]["message"]
    sync_users.assert_awaited_once()


@pytest.mark.asyncio
async def test_landing_page_includes_monitoring_latency_fallback(monkeypatch):
    rendered: dict[str, object] = {}

    def fake_render(request, template_name: str, **context):
        rendered["template_name"] = template_name
        rendered["context"] = context
        return context

    async def fake_scalar(_query):
        return SimpleNamespace(
            value={
                "checked_at": "2026-05-22T12:00:00+00:00",
                "servers": {
                    "server-1": {
                        "reachable": True,
                        "latency_ms": 73,
                        "probe_target_host": "wl.altlink.online",
                        "probe_target_port": 44443,
                        "checked_at": "2026-05-22T12:00:00+00:00",
                    }
                },
            }
        )

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            dashboard=SimpleNamespace(
                list_plans=AsyncMock(
                    return_value=[
                        plan(
                            PlanCode.UNLIMITED,
                            sort_order=20,
                            price_rub="199.00",
                            period_days=30,
                            description="unlimited",
                            device_limit=8,
                        )
                    ]
                )
            ),
            catalog=SimpleNamespace(
                list_servers=AsyncMock(
                    return_value=[
                        SimpleNamespace(
                            id="server-1",
                            name="Whitelist NL",
                            country_code="NL",
                            is_available=True,
                        )
                    ]
                )
            ),
            session=SimpleNamespace(scalar=fake_scalar),
        )

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(hub=fake_hub),
                settings=SimpleNamespace(
                    backend_public_url="https://altlink.online",
                    client_bot_name="@altlink_bot",
                    support_username="@altlink_support",
                ),
            )
        )
    )

    monkeypatch.setattr(web_routes, "render", fake_render)

    response = await web_routes.landing_page(request)

    assert response is rendered["context"]
    assert rendered["template_name"] == "landing.html"
    assert rendered["context"]["title"] == "ALTLINK VPN — быстрый и конфиденциальный доступ"
    assert rendered["context"]["landing_portal_authenticated"] is False
    assert rendered["context"]["landing_account_button_label"] == "Войти"
    assert rendered["context"]["support_url"] == "https://t.me/altlink_support"
    assert rendered["context"]["landing_max_device_limit"] == 8
    assert rendered["context"]["portal_plan_groups"][0]["periods"][0]["price_label"] == "199"
    assert rendered["context"]["landing_latency_best_label"] == "73 мс"
    assert rendered["context"]["landing_latency_checked_at"] == "2026-05-22T12:00:00+00:00"
    assert rendered["context"]["landing_latency_items"] == [
        {
            "server_id": "server-1",
            "name": "Whitelist NL",
            "country_code": "NL",
            "country_name": "Нидерланды",
            "country_flag": "🇳🇱",
            "reachable": True,
            "latency_ms": 73,
            "display_label": "73 мс",
            "display_state": "ready",
            "probe_target_host": "wl.altlink.online",
            "checked_at": "2026-05-22T12:00:00+00:00",
        }
    ]
    assert rendered["context"]["landing_location_items"] == [
        {
            "country_code": "NL",
            "country_name": "Нидерланды",
            "country_flag": "🇳🇱",
            "latency_ms": 73,
            "display_label": "73 мс",
            "display_state": "ready",
            "quality_label": "Стабильное соединение",
            "server_count": 1,
        }
    ]


@pytest.mark.asyncio
async def test_landing_page_uses_cabinet_button_for_authenticated_portal_user(monkeypatch):
    rendered: dict[str, object] = {}

    def fake_render(request, template_name: str, **context):
        rendered["template_name"] = template_name
        rendered["context"] = context
        return context

    async def fake_scalar(_query):
        return None

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            dashboard=SimpleNamespace(list_plans=AsyncMock(return_value=[])),
            catalog=SimpleNamespace(list_servers=AsyncMock(return_value=[])),
            accounts=SimpleNamespace(get_user=AsyncMock(return_value=SimpleNamespace(id="user-1"))),
            session=SimpleNamespace(scalar=fake_scalar),
        )

    request = SimpleNamespace(
        session={"portal_user_id": "user-1"},
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(hub=fake_hub),
                settings=SimpleNamespace(
                    backend_public_url="https://altlink.online",
                    client_bot_name="@altlink_bot",
                    support_username="@altlink_support",
                ),
            )
        ),
    )

    monkeypatch.setattr(web_routes, "render", fake_render)

    response = await web_routes.landing_page(request)

    assert response is rendered["context"]
    assert rendered["context"]["portal_login_url"] == "/portal"
    assert rendered["context"]["landing_portal_authenticated"] is True
    assert rendered["context"]["landing_account_button_label"] == "Личный кабинет"


def test_strip_document_title_removes_duplicate_first_h1():
    markdown = "# Пользовательское соглашение Altlink VPN\n\n## Раздел\n\nТекст"

    assert web_routes.strip_document_title(markdown) == "## Раздел\n\nТекст"


@pytest.mark.asyncio
async def test_build_portal_context_includes_server_latency_state(monkeypatch):
    async def fake_portal_channel_state(request, user):
        return True

    async def fake_scalar(_query):
        return SimpleNamespace(
            value={
                "checked_at": "2026-05-19T12:00:00+00:00",
                "servers": {
                    "server-1": {
                        "reachable": True,
                        "latency_ms": 64,
                        "probe_target_host": "wl.altlink.online",
                        "probe_target_port": 44443,
                        "checked_at": "2026-05-19T12:00:00+00:00",
                    }
                },
            }
        )

    monkeypatch.setattr(web_routes, "portal_channel_state", fake_portal_channel_state)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(
                    backend_public_url="https://altlink.online",
                    client_bot_name="@altlink_bot",
                    required_subscription_channel_url=None,
                    whitelist_price_per_gb_rub=Decimal("4"),
                )
            )
        )
    )
    hub = SimpleNamespace(
        accounts=SimpleNamespace(
            get_subscription_bundle=AsyncMock(return_value={"subscription": None, "subscription_info": None, "connection_keys": None}),
            can_offer_trial=AsyncMock(return_value=False),
            list_user_hwid_devices=AsyncMock(return_value=[]),
        ),
        catalog=SimpleNamespace(
            get_user_servers=AsyncMock(return_value=[SimpleNamespace(server=SimpleNamespace(id="server-1", name="Whitelist EU"))])
        ),
        dashboard=SimpleNamespace(list_plans=AsyncMock(return_value=[])),
        topups=SimpleNamespace(list_requests=AsyncMock(return_value=[])),
        session=SimpleNamespace(scalar=fake_scalar),
    )

    context = await build_portal_context(request, hub, SimpleNamespace(id="user-1"))

    assert context["portal_server_latency_checked_at"] == "2026-05-19T12:00:00+00:00"
    assert context["portal_server_latency_state"]["server-1"]["latency_ms"] == 64
    assert context["portal_server_latency_state"]["server-1"]["probe_target_host"] == "wl.altlink.online"


@pytest.mark.asyncio
async def test_settings_page_includes_whitelist_server_domain(monkeypatch):
    rendered: dict[str, object] = {}

    async def fake_resolve_admin(request, hub):
        return SimpleNamespace(id="admin-1", username="admin")

    def fake_render(request, template_name: str, **context):
        rendered["template_name"] = template_name
        rendered["context"] = context
        return context

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            dashboard=SimpleNamespace(
                list_settings=AsyncMock(
                    return_value=[
                        SimpleNamespace(
                            key=WHITELIST_SERVER_DOMAIN_SETTING_KEY,
                            value="https://wl.altlink.online/status",
                            description="demo",
                        )
                    ]
                )
            )
        )

    request = SimpleNamespace(
        session={"admin_id": "admin-1"},
        app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(hub=fake_hub))),
    )

    monkeypatch.setattr(web_routes, "resolve_admin", fake_resolve_admin)
    monkeypatch.setattr(web_routes, "render", fake_render)

    response = await web_routes.settings_page(request)

    assert response is rendered["context"]
    assert rendered["template_name"] == "settings.html"
    assert rendered["context"]["whitelist_server_domain_key"] == WHITELIST_SERVER_DOMAIN_SETTING_KEY
    assert rendered["context"]["whitelist_server_domain_value"] == "wl.altlink.online"


@pytest.mark.asyncio
async def test_settings_page_uses_legacy_whitelist_latency_key_as_fallback(monkeypatch):
    rendered: dict[str, object] = {}

    async def fake_resolve_admin(request, hub):
        return SimpleNamespace(id="admin-1", username="admin")

    def fake_render(request, template_name: str, **context):
        rendered["template_name"] = template_name
        rendered["context"] = context
        return context

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            dashboard=SimpleNamespace(
                list_settings=AsyncMock(
                    return_value=[
                        SimpleNamespace(
                            key=LEGACY_WHITELIST_LATENCY_TARGET_SETTING_KEY,
                            value="legacy-wl.altlink.online",
                            description="legacy",
                        )
                    ]
                )
            )
        )

    request = SimpleNamespace(
        session={"admin_id": "admin-1"},
        app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(hub=fake_hub))),
    )

    monkeypatch.setattr(web_routes, "resolve_admin", fake_resolve_admin)
    monkeypatch.setattr(web_routes, "render", fake_render)

    response = await web_routes.settings_page(request)

    assert response is rendered["context"]
    assert rendered["context"]["whitelist_server_domain_value"] == "legacy-wl.altlink.online"


@pytest.mark.asyncio
async def test_admin_support_reply_queues_notification_with_reply_button(monkeypatch):
    request_id = "12345678-1234-1234-1234-123456789abc"
    notifications = SimpleNamespace(queue=AsyncMock())
    support = SimpleNamespace(
        add_admin_message=AsyncMock(),
        get_request=AsyncMock(
            return_value=SimpleNamespace(id=request_id, user_id="user-1")
        ),
    )

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(support=support, notifications=notifications)

    async def fake_resolve_admin(request, hub):
        return SimpleNamespace(id="admin-1", username="admin")

    class DummyRequest(SimpleNamespace):
        async def form(self):
            return {"csrf_token": "token", "message": "Проверьте подключение ещё раз."}

    request = DummyRequest(
        session={"admin_id": "admin-1", "csrf_token": "token"},
        app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(hub=fake_hub))),
    )
    monkeypatch.setattr(web_routes, "resolve_admin", fake_resolve_admin)

    response = await web_routes.admin_support_reply(request, request_id)

    assert response.status_code == 303
    support.add_admin_message.assert_awaited_once()
    notifications.queue.assert_awaited_once()
    assert notifications.queue.await_args.kwargs["payload"] == {
        "cta": "support_reply",
        "support_request_id": request_id,
    }


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
