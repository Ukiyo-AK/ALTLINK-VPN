from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from altlink.application.services.monitoring import MonitoringAlert
from altlink.domain.enums import ServerType
from altlink.scheduler import jobs as scheduler_jobs


@pytest.mark.asyncio
async def test_user_abuse_monitor_job_ignores_many_active_ip_alerts(monkeypatch):
    sent_messages: list[dict] = []
    alert = MonitoringAlert(
        kind="user_many_active_ips",
        subject="@demo",
        details={
            "telegram_id": 43001,
            "unique_ip_count": 5,
            "unique_ip_threshold": 5,
            "unique_ips": ["203.0.113.1", "203.0.113.2"],
        },
    )

    async def fake_send_telegram_messages(*, bot_token: str, chat_ids, text: str, reply_markup=None):
        sent_messages.append({"bot_token": bot_token, "chat_ids": list(chat_ids), "text": text})
        return 1

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            monitoring=SimpleNamespace(capture_user_abuse_state=AsyncMock(return_value=[alert])),
            accounts=SimpleNamespace(list_admin_telegram_ids=AsyncMock(return_value=[44001])),
        )

    monkeypatch.setattr(scheduler_jobs, "send_telegram_messages", fake_send_telegram_messages)
    container = SimpleNamespace(
        hub=fake_hub,
        settings=SimpleNamespace(admin_bot_token="admin-token", client_bot_token="client-token"),
    )

    await scheduler_jobs.user_abuse_monitor_job(container)

    assert sent_messages == []


@pytest.mark.asyncio
async def test_server_latency_job_uses_manual_domain_for_whitelist_servers(monkeypatch):
    calls: list[tuple[str, str | None, int | None]] = []

    whitelist_server = SimpleNamespace(
        id="server-1",
        name="Whitelist NL",
        address="wl.example.com",
        country_code="NL",
        server_type=ServerType.WHITELIST,
        is_available=True,
        is_connected=True,
        inbounds=[SimpleNamespace(is_active=True, remnawave_inbound_uuid="in-1")],
    )
    regular_server = SimpleNamespace(
        id="server-2",
        name="Regular FI",
        address="fi.example.com",
        country_code="FI",
        server_type=ServerType.REGULAR,
        is_available=True,
        is_connected=True,
        inbounds=[SimpleNamespace(is_active=True, remnawave_inbound_uuid="in-2")],
    )

    async def fake_probe_server_latency(server, *, timeout_seconds: float = 2.5, override_host=None, override_port=None):
        calls.append((server.name, override_host, override_port))
        return {
            "name": server.name,
            "country_code": server.country_code,
            "latency_ms": 120,
            "reachable": True,
            "probe_target_host": override_host or server.address,
            "probe_target_port": override_port or 443,
        }

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            catalog=SimpleNamespace(list_servers=AsyncMock(return_value=[whitelist_server, regular_server])),
            session=SimpleNamespace(
                scalars=AsyncMock(
                    return_value=SimpleNamespace(
                        all=lambda: [SimpleNamespace(key="monitoring.whitelist_server_domain", value="https://wl.altlink.online/path")]
                    )
                )
            ),
            monitoring=SimpleNamespace(record_server_latency_state=AsyncMock(return_value=[])),
        )

    monkeypatch.setattr(scheduler_jobs, "probe_server_latency", fake_probe_server_latency)

    container = SimpleNamespace(
        hub=fake_hub,
        settings=SimpleNamespace(admin_bot_token="admin-token"),
    )

    await scheduler_jobs.server_latency_job(container)

    assert ("Whitelist NL", "wl.altlink.online", None) in calls
    assert ("Regular FI", None, None) in calls


@pytest.mark.asyncio
async def test_user_abuse_monitor_job_sends_alerts_only_through_admin_bot(monkeypatch):
    sent_messages: list[dict] = []
    alert = MonitoringAlert(
        kind="user_hwid_limit_exceeded",
        subject="@demo",
        details={
            "telegram_id": 43001,
            "hwid_device_count": 3,
            "device_limit": 2,
        },
    )

    async def fake_send_telegram_messages(*, bot_token: str, chat_ids, text: str, reply_markup=None):
        sent_messages.append({"bot_token": bot_token, "chat_ids": list(chat_ids), "text": text})
        return 1

    @asynccontextmanager
    async def fake_hub():
        yield SimpleNamespace(
            monitoring=SimpleNamespace(capture_user_abuse_state=AsyncMock(return_value=[alert])),
            accounts=SimpleNamespace(list_admin_telegram_ids=AsyncMock(return_value=[44001])),
        )

    monkeypatch.setattr(scheduler_jobs, "send_telegram_messages", fake_send_telegram_messages)
    container = SimpleNamespace(
        hub=fake_hub,
        settings=SimpleNamespace(admin_bot_token="admin-token", client_bot_token="client-token"),
    )

    await scheduler_jobs.user_abuse_monitor_job(container)

    assert sent_messages == [
        {
            "bot_token": "admin-token",
            "chat_ids": [44001],
            "text": "⚠️ Антиабуз: обнаружена подозрительная активность\n\n"
            "• @demo (Telegram ID: 43001)\n"
            "  HWID-устройств: 3 при лимите 2",
        }
    ]
