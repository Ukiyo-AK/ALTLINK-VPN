from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from altlink.domain.billing import evaluate_renewal


def test_renewal_enters_grace_when_balance_is_low():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    decision = evaluate_renewal(
        balance_rub=Decimal("10"),
        price_rub=Decimal("100"),
        now=now,
        grace_started_at=None,
        grace_days=14,
    )
    assert decision.action == "enter_grace"
    assert decision.grace_until == now + timedelta(days=14)


def test_renewal_blocks_after_grace_timeout():
    now = datetime(2026, 1, 20, tzinfo=UTC)
    decision = evaluate_renewal(
        balance_rub=Decimal("10"),
        price_rub=Decimal("100"),
        now=now,
        grace_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        grace_days=14,
    )
    assert decision.action == "block"

