from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from time import monotonic


class AnalyticsCache:
    """Small process-local cache; authentication and HTML rendering stay outside it."""

    def __init__(self, *, ttl_seconds: float = 30, max_entries: int = 8):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key, loader):
        async with self._lock:
            now = monotonic()
            for expired in [key for key, (until, _) in self._entries.items() if until <= now]:
                del self._entries[expired]
            if key in self._entries:
                self._entries.move_to_end(key)
                return deepcopy(self._entries[key][1])
            value = await loader()
            self._entries[key] = (monotonic() + self.ttl_seconds, deepcopy(value))
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            return value


def business_snapshot(overview: dict) -> dict:
    # Keep no ORM objects/sessions or raw user records in the cache.
    return {
        **overview,
        "servers": [],
        "top_users": [
            {
                "user": {"username": row.user.username, "telegram_id": row.user.telegram_id},
                "plan": {"name": row.plan.name} if row.plan else None,
                "traffic_used_bytes": row.traffic_used_bytes,
            }
            for row in overview.get("top_users", [])
        ],
        "recent_topups": [
            {
                "user": {"username": row.user.username, "telegram_id": row.user.telegram_id},
                "amount_rub": row.amount_rub,
                "created_at": row.created_at,
            }
            for row in overview.get("recent_topups", [])
        ],
    }


def infrastructure_snapshot(result: dict) -> dict:
    def server_fields(server):
        return {name: getattr(server, name) for name in (
            "id", "name", "country_code", "server_type", "current_clients", "users_online",
        )}

    return {
        **result,
        "server_options": [server_fields(server) for server in result.get("server_options", [])],
        "uptime_cards": [
            {**card, "server": server_fields(card["server"])}
            for card in result.get("uptime_cards", [])
        ],
    }
