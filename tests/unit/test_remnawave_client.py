from __future__ import annotations

from datetime import UTC, datetime

import pytest

from altlink.infrastructure.remnawave_client import RemnawaveClient
from altlink.settings import Settings


@pytest.mark.asyncio
async def test_get_node_user_usage_falls_back_to_legacy_nodes_usage_route(monkeypatch):
    client = RemnawaveClient(
        Settings(
            _env_file=None,
            remnawave_base_url="https://remna.example",
            remnawave_api_token="token",
        )
    )

    calls: list[str] = []

    async def fake_request(method: str, path: str, **kwargs):
        calls.append(path)
        if path == "/api/bandwidth-stats/nodes/node-1/users/legacy":
            return None
        if path == "/api/nodes/usage/node-1/users/range":
            return [
                {
                    "userUuid": "user-1",
                    "username": "demo",
                    "nodeUuid": "node-1",
                    "total": 1234,
                    "date": "2026-05-04",
                }
            ]
        raise AssertionError(f"Unexpected path: {path}")

    monkeypatch.setattr(client, "_request", fake_request)

    try:
        rows = await client.get_node_user_usage(
            "node-1",
            datetime(2026, 5, 4, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        )
    finally:
        await client.aclose()

    assert calls == [
        "/api/bandwidth-stats/nodes/node-1/users/legacy",
        "/api/nodes/usage/node-1/users/range",
    ]
    assert rows is not None
    assert len(rows) == 1
    assert rows[0].userUuid == "user-1"
    assert rows[0].total == 1234


@pytest.mark.asyncio
async def test_hwid_device_methods_use_remnawave_contract(monkeypatch):
    client = RemnawaveClient(
        Settings(
            _env_file=None,
            remnawave_base_url="https://remna.example",
            remnawave_api_token="token",
        )
    )
    calls: list[tuple[str, str, dict]] = []
    payload = {
        "devices": [
            {
                "hwid": "hwid-1",
                "userUuid": "609237c1-7ffb-4d76-9861-a14b7ddc8a6a",
                "platform": "Android",
                "osVersion": "14",
                "deviceModel": "Pixel",
                "userAgent": "Happ/1.0",
                "createdAt": "2026-05-30T10:00:00Z",
                "updatedAt": "2026-05-31T11:00:00Z",
            }
        ]
    }

    async def fake_request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        return payload

    monkeypatch.setattr(client, "_request", fake_request)
    try:
        devices = await client.get_user_hwid_devices("user-1")
        remaining = await client.delete_user_hwid_device("user-1", "hwid-1")
    finally:
        await client.aclose()

    assert devices[0].deviceModel == "Pixel"
    assert remaining[0].userAgent == "Happ/1.0"
    assert calls == [
        ("GET", "/api/hwid/devices/user-1", {}),
        ("POST", "/api/hwid/devices/delete", {"json": {"userUuid": "user-1", "hwid": "hwid-1"}}),
    ]
