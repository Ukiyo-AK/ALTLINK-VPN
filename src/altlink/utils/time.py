from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


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


def parse_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_moscow(dt: datetime | str) -> datetime:
    return ensure_utc(parse_datetime(dt)).astimezone(MOSCOW_TZ)


def format_msk_datetime(dt: datetime | str | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if dt is None:
        return "—"
    try:
        return f"{to_moscow(dt).strftime(fmt)} МСК"
    except (TypeError, ValueError):
        return "—"


def format_msk_date(dt: datetime | str | None) -> str:
    if dt is None:
        return "—"
    try:
        return f"{to_moscow(dt).strftime('%d.%m.%Y')} МСК"
    except (TypeError, ValueError):
        return "—"
