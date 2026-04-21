from __future__ import annotations

from altlink.infrastructure.remnawave_schemas import RemoteNode


def test_remote_node_accepts_integer_xray_uptime() -> None:
    node = RemoteNode.model_validate(
        {
            "uuid": "node-1",
            "name": "Finland 1",
            "address": "finland.example.com",
            "port": 443,
            "isConnected": True,
            "isDisabled": False,
            "isConnecting": False,
            "lastStatusChange": "2026-04-20T17:36:17.269Z",
            "lastStatusMessage": "ok",
            "xrayUptime": 2457,
            "isTrafficTrackingActive": True,
            "trafficResetDay": 1,
            "trafficUsedBytes": 0,
            "notifyPercent": 80,
            "usersOnline": 1,
            "viewPosition": 1,
            "countryCode": "FI",
            "consumptionMultiplier": 1,
            "tags": [],
            "createdAt": "2026-04-20T17:36:17.269Z",
            "updatedAt": "2026-04-20T17:36:17.269Z",
            "configProfile": {
                "activeConfigProfileUuid": "profile-1",
                "activeInbounds": [],
            },
        }
    )

    assert node.xrayUptime == 2457
