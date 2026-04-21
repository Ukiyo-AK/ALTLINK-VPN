from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime

import httpx

logger = logging.getLogger(__name__)


def verify_telegram_auth_payload(
    payload: dict[str, str],
    *,
    bot_token: str,
    max_age_seconds: int,
) -> bool:
    auth_hash = payload.get("hash")
    auth_date = payload.get("auth_date")
    if not auth_hash or not auth_date:
        return False

    try:
        auth_ts = int(auth_date)
    except (TypeError, ValueError):
        return False

    now_ts = int(datetime.now(UTC).timestamp())
    if now_ts - auth_ts > max_age_seconds:
        return False

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(payload.items())
        if key != "hash" and value not in (None, "")
    )
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    computed_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed_hash, auth_hash)


async def check_channel_membership(
    *,
    bot_token: str,
    channel: str,
    user_id: int,
) -> bool:
    if not bot_token or not channel:
        return True

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"https://api.telegram.org/bot{bot_token}/getChatMember",
                params={"chat_id": channel, "user_id": user_id},
            )
            payload = response.json()
            if not response.is_success or not payload.get("ok"):
                logger.warning(
                    "Telegram channel membership check failed for channel %s and user %s: %s",
                    channel,
                    user_id,
                    payload.get("description") if isinstance(payload, dict) else response.text,
                )
                return False
            status = (payload.get("result") or {}).get("status")
            return status in {"creator", "administrator", "member", "restricted"}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "Telegram channel membership check errored for channel %s and user %s: %s",
            channel,
            user_id,
            exc,
        )
        return False
