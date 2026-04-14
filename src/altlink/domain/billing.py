from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal


def compute_period_end(started_at: datetime, period_days: int) -> datetime:
    return started_at + timedelta(days=period_days)


def compute_grace_deadline(started_at: datetime, grace_days: int) -> datetime:
    return started_at + timedelta(days=grace_days)


def normalize_thresholds(thresholds: list[int]) -> list[int]:
    return sorted({threshold for threshold in thresholds if 0 < threshold <= 100})


def detect_threshold_crossing(
    used_bytes: int,
    limit_bytes: int | None,
    thresholds: list[int],
    last_threshold: int,
) -> int | None:
    if not limit_bytes or limit_bytes <= 0:
        return None
    used_percent = int((used_bytes / limit_bytes) * 100)
    crossed = [threshold for threshold in normalize_thresholds(thresholds) if last_threshold < threshold <= used_percent]
    return max(crossed) if crossed else None


@dataclass(slots=True)
class RenewalDecision:
    action: str
    grace_until: datetime | None = None


def evaluate_renewal(
    *,
    balance_rub: Decimal,
    price_rub: Decimal,
    now: datetime,
    grace_started_at: datetime | None,
    grace_days: int,
) -> RenewalDecision:
    if balance_rub >= price_rub:
        return RenewalDecision(action="charge")
    if grace_started_at is None:
        return RenewalDecision(action="enter_grace", grace_until=compute_grace_deadline(now, grace_days))
    if now < compute_grace_deadline(grace_started_at, grace_days):
        return RenewalDecision(action="keep_grace", grace_until=compute_grace_deadline(grace_started_at, grace_days))
    return RenewalDecision(action="block")

