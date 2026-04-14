from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def localize(dt: datetime, tz_name: str) -> datetime:
    zone = ZoneInfo(tz_name)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC).astimezone(zone)
    return dt.astimezone(zone)
