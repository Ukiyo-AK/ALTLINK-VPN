from __future__ import annotations

from decimal import Decimal

from altlink.domain.plans import (
    SINGLE_10GBIT_MONTHLY_PRICE_RUB,
    SINGLE_10GBIT_WEEKLY_PRICE_RUB,
    UNLIMITED_MONTHLY_PRICE_RUB,
    UNLIMITED_WEEKLY_PRICE_RUB,
)


def test_weekly_single_server_price_is_rounded_to_clean_customer_value():
    assert SINGLE_10GBIT_MONTHLY_PRICE_RUB == Decimal("69")
    assert SINGLE_10GBIT_WEEKLY_PRICE_RUB == Decimal("25")


def test_weekly_unlimited_price_is_rounded_to_clean_customer_value():
    assert UNLIMITED_MONTHLY_PRICE_RUB == Decimal("199")
    assert UNLIMITED_WEEKLY_PRICE_RUB == Decimal("65")
