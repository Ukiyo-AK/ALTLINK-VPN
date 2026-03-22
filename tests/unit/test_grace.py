from __future__ import annotations

from decimal import Decimal

from altlink.domain.policies import should_enter_grace


def test_enters_grace_when_balance_insufficient():
    assert should_enter_grace(Decimal("49.00"), Decimal("50.00")) is True


def test_does_not_enter_grace_when_balance_is_enough():
    assert should_enter_grace(Decimal("50.00"), Decimal("50.00")) is False

