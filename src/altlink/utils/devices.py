from __future__ import annotations

from hashlib import blake2s

from altlink.infrastructure.remnawave_schemas import RemoteHwidDevice


def hwid_device_name(device: RemoteHwidDevice) -> str:
    return device.deviceModel or device.platform or "Неизвестное устройство"


def hwid_device_client(device: RemoteHwidDevice) -> str:
    return device.userAgent or "Не определён"


def hwid_device_fingerprint(device: RemoteHwidDevice) -> str:
    return blake2s(device.hwid.encode("utf-8"), digest_size=6).hexdigest()


def hwid_device_view(device: RemoteHwidDevice) -> dict[str, object]:
    return {
        "hwid": device.hwid,
        "name": hwid_device_name(device),
        "client": hwid_device_client(device),
        "platform": device.platform or "Не определена",
        "os_version": device.osVersion or "Не определена",
        "last_connected_at": device.updatedAt,
        "created_at": device.createdAt,
        "fingerprint": hwid_device_fingerprint(device),
    }
