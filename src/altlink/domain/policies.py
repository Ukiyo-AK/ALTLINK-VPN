from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from altlink.domain.enums import UserStatus


def can_start_trial(user_status: UserStatus, has_existing_trial: bool) -> bool:
    return user_status == UserStatus.NEW and not has_existing_trial


def has_enough_balance(balance_rub: Decimal, price_rub: Decimal) -> bool:
    return balance_rub >= price_rub


def should_enter_grace(balance_rub: Decimal, renewal_price_rub: Decimal) -> bool:
    return balance_rub < renewal_price_rub


def calculate_next_period(anchor: datetime, duration_days: int) -> tuple[datetime, datetime]:
    return anchor, anchor + timedelta(days=duration_days)


def calculate_server_load_percent(current_clients: int, max_clients: int) -> Decimal:
    if max_clients <= 0:
        max_clients = 1
    return Decimal(current_clients) / Decimal(max_clients) * Decimal("100.00")


def traffic_percent(used_bytes: int, limit_bytes: int | None) -> float:
    if not limit_bytes or limit_bytes <= 0:
        return 0.0
    return used_bytes / limit_bytes * 100


def reached_thresholds(used_bytes: int, limit_bytes: int | None, thresholds: list[int]) -> list[int]:
    percent = traffic_percent(used_bytes, limit_bytes)
    return [threshold for threshold in thresholds if percent >= threshold]


def should_block_on_traffic_limit(used_bytes: int, limit_bytes: int | None) -> bool:
    if not limit_bytes or limit_bytes <= 0:
        return False
    return used_bytes >= limit_bytes
