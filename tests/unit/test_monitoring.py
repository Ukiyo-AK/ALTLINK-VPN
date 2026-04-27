from __future__ import annotations

import pytest


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
        }
        unhealthy_probe = {
            "server_id": server.id,
            "name": server.name,
            "address": server.address,
            "country_code": server.country_code,
            "reachable": False,
            "latency_ms": None,
            "error": "timeout",
        }

        assert await hub.monitoring.record_server_latency_state([healthy_probe]) == []

        alerts = await hub.monitoring.record_server_latency_state([unhealthy_probe])
        assert len(alerts) == 1
        assert alerts[0].subject == server.name

        repeat_alerts = await hub.monitoring.record_server_latency_state([unhealthy_probe])
        assert repeat_alerts == []
