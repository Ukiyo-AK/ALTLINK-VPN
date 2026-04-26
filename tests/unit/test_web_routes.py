from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from altlink.domain.enums import PlanCode
from altlink.presentation.web.routes import (
    group_portal_plans,
    is_foreign_latency_target,
    latency_probe,
    load_document_text,
    probe_server_latency,
    resolve_document_path,
    server_probe_port,
    LATENCY_RECHECK_THRESHOLD_MS,
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
            plan(PlanCode.UNLIMITED_WEEKLY, sort_order=25, price_rub="64.68", period_days=7, description="unlimited weekly", device_limit=8),
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
        SimpleNamespace(name="RU Main", country_code="RU", is_available=True, is_connected=True, inbounds=[]),
        SimpleNamespace(name="NL Node", country_code="NL", is_available=True, is_connected=True, inbounds=[]),
        SimpleNamespace(name="DE Node", country_code="DE", is_available=True, is_connected=True, inbounds=[]),
    ]

    async def fake_probe(server, *, timeout_seconds: float = 2.5):
        if server.name == "NL Node":
            return {"name": server.name, "country_code": server.country_code, "latency_ms": 42, "reachable": True}
        return {"name": server.name, "country_code": server.country_code, "latency_ms": None, "reachable": False}

    async def fake_list_servers():
        return servers

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(catalog=SimpleNamespace(list_servers=fake_list_servers))

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(hub=fake_hub))))
    monkeypatch.setattr("altlink.presentation.web.routes.probe_server_latency", fake_probe)

    response = await latency_probe(request)

    assert response.status_code == 200
    assert response.headers["Cache-Control"].startswith("no-store")
    assert b"foreign_servers" in response.body
    assert b"recheck_threshold_ms" in response.body
    assert b"disclaimer" in response.body
    assert b"NL Node" in response.body
    assert b"RU Main" not in response.body
