from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from sqlalchemy import select

from altlink.application.services.accounts import UserListFilters
from altlink.infrastructure.db.models import SystemEvent, SystemSetting, TrafficSnapshot
from altlink.utils.time import MOSCOW_TZ, utc_now


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
async def test_record_user_abuse_state_alerts_only_on_new_or_increased_device_excess(test_services):
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
            [{**base_observation, "hwid_device_count": 4}]
        )
        await hub.monitoring.record_user_abuse_state(
            [{**base_observation, "ips": [], "hwid_device_count": 2}]
        )
        repeated_after_recovery = await hub.monitoring.record_user_abuse_state([base_observation])

    assert [item.kind for item in first_alerts] == ["user_hwid_limit_exceeded"]
    assert repeat_alerts == []
    assert [item.kind for item in increased_alerts] == ["user_hwid_limit_exceeded"]
    assert [item.kind for item in repeated_after_recovery] == ["user_hwid_limit_exceeded"]


@pytest.mark.asyncio
async def test_record_user_abuse_state_preserves_device_alert_when_remote_data_is_temporarily_unavailable(test_services):
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
    assert snapshot.value["users"]["user-1"]["device_alert_active"] is True


@pytest.mark.asyncio
async def test_record_user_abuse_state_tracks_monthly_threshold_per_calendar_month(test_services):
    observation = {
        "user_id": "user-monthly",
        "telegram_id": 42005,
        "username": "monthly_traffic",
        "hwid_device_count": 0,
        "device_limit": 2,
        "daily_traffic_bytes": 1 * 1024**3,
        "daily_traffic_threshold_bytes": 50 * 1024**3,
        "monthly_traffic_bytes": 1025 * 1024**3,
        "monthly_traffic_threshold_bytes": 1024 * 1024**3,
        "daily_period": "2026-07-22",
        "monthly_period": "2026-07",
    }

    async with test_services.hub() as hub:
        first = await hub.monitoring.record_user_abuse_state([observation])
        repeated = await hub.monitoring.record_user_abuse_state([observation])
        next_month = await hub.monitoring.record_user_abuse_state(
            [{**observation, "daily_period": "2026-08-01", "monthly_period": "2026-08"}]
        )

    assert [item.kind for item in first] == ["user_traffic_anomaly"]
    assert repeated == []
    assert [item.kind for item in next_month] == ["user_traffic_anomaly"]


@pytest.mark.asyncio
async def test_capture_user_abuse_state_skips_live_ips_and_collects_hwid_devices(test_services):
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
        page = await hub.accounts.list_users_for_admin(
            UserListFilters(search="live_abuse", sort="devices", direction="desc", limit=5)
        )

    assert [item.kind for item in alerts] == ["user_hwid_limit_exceeded"]
    assert all(item.details["telegram_id"] == 42002 for item in alerts)
    assert page.users[0].admin_device_count == 9
    assert page.users[0].hwid_devices_checked_at is not None
    assert test_services.remnawave.ip_control_jobs == {}


@pytest.mark.asyncio
async def test_capture_user_abuse_state_alerts_once_for_daily_traffic_and_adds_server_stats(test_services):
    now = utc_now()
    day_start = now.astimezone(MOSCOW_TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=42004,
            username="traffic_anomaly",
            first_name="Traffic",
            last_name="Anomaly",
            language_code="ru",
        )
        subscription = await hub.billing.activate_trial(user.id)
        hub.session.add_all(
            [
                TrafficSnapshot(
                    user_id=user.id,
                    subscription_id=subscription.id,
                    server_id=None,
                    snapshot_date=(day_start - timedelta(seconds=1)).date(),
                    used_bytes=0,
                    lifetime_used_bytes=10 * 1024**3,
                    source="test",
                    created_at=day_start - timedelta(seconds=1),
                ),
                TrafficSnapshot(
                    user_id=user.id,
                    subscription_id=subscription.id,
                    server_id=None,
                    snapshot_date=now.date(),
                    used_bytes=60 * 1024**3,
                    lifetime_used_bytes=70 * 1024**3,
                    source="test",
                    created_at=now,
                ),
            ]
        )
        await hub.session.flush()

        first_alerts = await hub.monitoring.capture_user_abuse_state()
        repeated_alerts = await hub.monitoring.capture_user_abuse_state()
        event = await hub.session.scalar(
            select(SystemEvent)
            .where(SystemEvent.subject_user_id == user.id, SystemEvent.event_type == "user_abuse_detected")
            .order_by(SystemEvent.created_at.desc())
        )

    traffic_alert = next(item for item in first_alerts if item.kind == "user_traffic_anomaly")
    assert traffic_alert.details["daily_traffic_bytes"] == 60 * 1024**3
    assert traffic_alert.details["server_stats"]
    assert repeated_alerts == []
    assert event is not None
    assert event.payload.get("server_stats")
