from __future__ import annotations

import pytest
from sqlalchemy import select

from altlink.infrastructure.db.models import SystemSetting


@pytest.mark.asyncio
async def test_capture_remnawave_status_toggles_client_maintenance(test_services, monkeypatch):
    async def healthcheck_down():
        return False

    async def healthcheck_up():
        return True

    monkeypatch.setattr(test_services.remnawave, "healthcheck", healthcheck_down)
    async with test_services.hub() as hub:
        state = await hub.monitoring.capture_remnawave_status()
        assert state["available"] is False
        assert state["became_unavailable"] is True
        assert await hub.monitoring.is_client_maintenance_active() is True

    monkeypatch.setattr(test_services.remnawave, "healthcheck", healthcheck_up)
    async with test_services.hub() as hub:
        state = await hub.monitoring.capture_remnawave_status()
        assert state["available"] is True
        assert state["recovered"] is True
        assert await hub.monitoring.is_client_maintenance_active() is False


@pytest.mark.asyncio
async def test_record_server_operational_state_alerts_only_on_new_outage(test_services):
    async with test_services.hub() as hub:
        servers = await hub.catalog.list_servers()
        assert await hub.monitoring.record_server_operational_state(servers) == []

        broken = servers[0]
        broken.is_connected = False

        alerts = await hub.monitoring.record_server_operational_state(servers)
        assert len(alerts) == 1
        assert alerts[0].subject == broken.name

        repeat_alerts = await hub.monitoring.record_server_operational_state(servers)
        assert repeat_alerts == []


@pytest.mark.asyncio
async def test_record_server_latency_state_alerts_only_on_new_unreachable_server(test_services):
    async with test_services.hub() as hub:
        server = (await hub.catalog.list_servers())[0]
        healthy_probe = {
            "server_id": server.id,
            "name": server.name,
            "address": server.address,
            "country_code": server.country_code,
            "reachable": True,
            "latency_ms": 42,
            "probe_target_host": "wl.altlink.online",
            "probe_target_port": 44443,
        }
        unhealthy_probe = {
            "server_id": server.id,
            "name": server.name,
            "address": server.address,
            "country_code": server.country_code,
            "reachable": False,
            "latency_ms": None,
            "error": "timeout",
            "probe_target_host": "wl.altlink.online",
            "probe_target_port": 44443,
        }

        assert await hub.monitoring.record_server_latency_state([healthy_probe]) == []

        alerts = await hub.monitoring.record_server_latency_state([unhealthy_probe])
        assert len(alerts) == 1
        assert alerts[0].subject == server.name

        repeat_alerts = await hub.monitoring.record_server_latency_state([unhealthy_probe])
        assert repeat_alerts == []
        snapshot = await hub.session.scalar(
            select(SystemSetting).where(SystemSetting.key == hub.monitoring.SERVER_LATENCY_STATUS_KEY)
        )
        assert snapshot.value["servers"][server.id]["probe_target_host"] == "wl.altlink.online"
        assert snapshot.value["servers"][server.id]["probe_target_port"] == 44443


@pytest.mark.asyncio
async def test_manual_client_maintenance_blocks_regular_users_but_allows_exceptions(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=41001,
            username="maintenance_tester",
            first_name="Maintenance",
            last_name="Tester",
            language_code="ru",
        )
        await hub.monitoring.set_manual_client_maintenance(True)

        assert await hub.monitoring.is_client_maintenance_active(telegram_id=41002) is True

        await hub.monitoring.add_manual_maintenance_exception(user)
        assert await hub.monitoring.is_client_maintenance_active(telegram_id=user.telegram_id) is False

        await hub.monitoring.remove_manual_maintenance_exception(user)
        assert await hub.monitoring.is_client_maintenance_active(telegram_id=user.telegram_id) is True


@pytest.mark.asyncio
async def test_record_user_abuse_state_alerts_only_on_new_or_increased_excess(test_services):
    test_services.settings.user_abuse_unique_ip_threshold = 3
    base_observation = {
        "user_id": "user-1",
        "telegram_id": 42001,
        "username": "shared_link",
        "ips": ["203.0.113.1", "203.0.113.2", "203.0.113.3"],
        "hwid_device_count": 3,
        "device_limit": 2,
    }

    async with test_services.hub() as hub:
        first_alerts = await hub.monitoring.record_user_abuse_state([base_observation])
        repeat_alerts = await hub.monitoring.record_user_abuse_state([base_observation])
        increased_alerts = await hub.monitoring.record_user_abuse_state(
            [{**base_observation, "ips": [*base_observation["ips"], "203.0.113.4"]}]
        )
        await hub.monitoring.record_user_abuse_state(
            [{**base_observation, "ips": [], "hwid_device_count": 2}]
        )
        repeated_after_recovery = await hub.monitoring.record_user_abuse_state([base_observation])

    assert {item.kind for item in first_alerts} == {"user_many_active_ips", "user_hwid_limit_exceeded"}
    assert repeat_alerts == []
    assert [item.kind for item in increased_alerts] == ["user_many_active_ips"]
    assert {item.kind for item in repeated_after_recovery} == {"user_many_active_ips", "user_hwid_limit_exceeded"}


@pytest.mark.asyncio
async def test_record_user_abuse_state_preserves_alerts_when_remote_data_is_temporarily_unavailable(test_services):
    test_services.settings.user_abuse_unique_ip_threshold = 3
    observation = {
        "user_id": "user-1",
        "telegram_id": 42003,
        "username": "temporary_failure",
        "ips": ["203.0.113.1", "203.0.113.2", "203.0.113.3"],
        "hwid_device_count": 3,
        "device_limit": 2,
    }

    async with test_services.hub() as hub:
        await hub.monitoring.record_user_abuse_state([observation])
        alerts = await hub.monitoring.record_user_abuse_state(
            [{**observation, "ips": None, "hwid_device_count": None}]
        )
        snapshot = await hub.session.scalar(
            select(SystemSetting).where(SystemSetting.key == hub.monitoring.USER_ABUSE_STATUS_KEY)
        )

    assert alerts == []
    assert snapshot.value["users"]["user-1"]["ip_alert_active"] is True
    assert snapshot.value["users"]["user-1"]["device_alert_active"] is True


@pytest.mark.asyncio
async def test_capture_user_abuse_state_collects_live_ips_and_hwid_devices(test_services):
    test_services.settings.user_abuse_unique_ip_threshold = 5
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=42002,
            username="live_abuse",
            first_name="Live",
            last_name="Abuse",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)
        remote_user = hub.remnawave.users[user.remnawave_user_uuid]
        node_uuid = next(iter(hub.remnawave.nodes))
        hub.remnawave.set_node_user_ips(
            node_uuid,
            remote_user.id,
            ["203.0.113.1", "203.0.113.2", "203.0.113.3", "203.0.113.4", "203.0.113.5"],
        )
        for index in range(9):
            hub.remnawave.add_hwid_device(user.remnawave_user_uuid, hwid=f"hwid-{index}")

        alerts = await hub.monitoring.capture_user_abuse_state()

    assert {item.kind for item in alerts} == {"user_many_active_ips", "user_hwid_limit_exceeded"}
    assert all(item.details["telegram_id"] == 42002 for item in alerts)
