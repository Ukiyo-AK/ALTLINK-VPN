from __future__ import annotations

import base64
from decimal import Decimal
from urllib.parse import quote, urlparse


def local_subscription_proxy_url(settings, short_uuid: str, client_type: str | None = None) -> str | None:
    public_url = (getattr(settings, "backend_public_url", "") or "").strip()
    parsed = urlparse(public_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not short_uuid:
        return None

    url = f"{public_url.rstrip('/')}/sub/{quote(short_uuid, safe='')}"
    if client_type:
        url = f"{url}/{quote(client_type, safe='')}"
    return url


def build_client_announce_text(user, subscription, settings) -> str:
    raw_status = getattr(user, "status", None)
    status_value = getattr(raw_status, "value", raw_status)
    status_label = {
        "trial": "тест активен",
        "active": "доступ активен",
        "grace": "льготный период",
        "blocked": "нужна оплата",
        "canceled": "продление выключено",
        "expired": "подписка истекла",
        "new": "ожидает активации",
    }.get(str(status_value or "").strip().lower(), "проверьте кабинет")

    plan = getattr(subscription, "plan", None)
    plan_name = getattr(plan, "name", None) or "не активен"
    device_limit = getattr(plan, "device_limit", None)
    device_label = f"до {device_limit}" if device_limit else "без лимита"

    return "\n".join(
        [
            "✨ ALTLINK VPN",
            f"🧾 Тариф: {plan_name}",
            f"💳 Баланс: {Decimal(getattr(user, 'balance_rub', 0) or 0):.2f} ₽",
            f"📱 Устройств: {device_label}",
            f"🔔 Статус: {status_label}",
        ]
    )


def decode_announce_header(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.startswith("base64:"):
        try:
            return base64.b64decode(raw.split(":", 1)[1]).decode("utf-8")
        except Exception:
            return None
    return raw


def encode_announce_header(text: str) -> str:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"base64:{encoded}"

