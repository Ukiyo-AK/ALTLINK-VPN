from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from altlink.domain.policies import calculate_next_period, has_enough_balance


def test_has_enough_balance_positive_case():
    assert has_enough_balance(Decimal("200.00"), Decimal("100.00")) is True


def test_has_enough_balance_negative_case():
    assert has_enough_balance(Decimal("99.99"), Decimal("100.00")) is False


def test_next_period_keeps_anchor():
    start = datetime(2026, 3, 22, tzinfo=UTC)
    period_start, period_end = calculate_next_period(start, 30)
    assert period_start == start
    assert (period_end - period_start).days == 30

