from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP


MONEY_STEP = Decimal("0.01")


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


def quantize_money(amount: Decimal) -> Decimal:
    return Decimal(amount).quantize(MONEY_STEP, rounding=ROUND_HALF_UP)


def compute_prorated_daily_charge(price_rub: Decimal, period_days: int, day_number: int) -> Decimal:
    if period_days <= 0:
        return Decimal("0.00")
    if day_number <= 0:
        return Decimal("0.00")
    current_total = quantize_money((Decimal(price_rub) * Decimal(day_number)) / Decimal(period_days))
    previous_total = quantize_money((Decimal(price_rub) * Decimal(day_number - 1)) / Decimal(period_days))
    return quantize_money(current_total - previous_total)


def bytes_to_gb_cost(used_bytes: int, price_per_gb: Decimal) -> Decimal:
    if used_bytes <= 0:
        return Decimal("0.00")
    gb = Decimal(used_bytes) / Decimal(1024**3)
    return quantize_money(gb * Decimal(price_per_gb))


@dataclass(slots=True)
class RenewalDecision:
    action: str
    grace_until: datetime | None = None


def evaluate_renewal(
    *,
    balance_rub: Decimal,
    charge_rub: Decimal,
    debt_rub: Decimal,
    now: datetime,
    grace_started_at: datetime | None,
    grace_days: int,
) -> RenewalDecision:
    due_total = quantize_money(Decimal(charge_rub) + Decimal(debt_rub))
    if balance_rub >= due_total:
        return RenewalDecision(action="charge")
    if grace_started_at is None:
        return RenewalDecision(action="enter_grace", grace_until=compute_grace_deadline(now, grace_days))
    if now < compute_grace_deadline(grace_started_at, grace_days):
        return RenewalDecision(action="keep_grace", grace_until=compute_grace_deadline(grace_started_at, grace_days))
    return RenewalDecision(action="block")
