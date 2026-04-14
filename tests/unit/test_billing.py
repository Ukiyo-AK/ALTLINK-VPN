from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from altlink.domain.billing import compute_period_end, evaluate_renewal


def test_compute_period_end_uses_fixed_day_window():
    started_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert compute_period_end(started_at, 30) == datetime(2026, 1, 31, 12, 0, tzinfo=UTC)


def test_renewal_charges_when_balance_is_enough():
    decision = evaluate_renewal(
        balance_rub=Decimal("200"),
        price_rub=Decimal("100"),
        now=datetime(2026, 1, 1, tzinfo=UTC),
        grace_started_at=None,
        grace_days=14,
    )
    assert decision.action == "charge"
    assert decision.grace_until is None

