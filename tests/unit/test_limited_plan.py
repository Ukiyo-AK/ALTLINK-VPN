from __future__ import annotations

from decimal import Decimal

from altlink.domain.billing import bytes_to_gb_cost
from altlink.domain.plans import WHITELIST_GB_PRICE_RUB


def test_whitelist_cost_is_calculated_per_gigabyte():
    one_and_half_gb = int(1.5 * 1024**3)
    assert bytes_to_gb_cost(one_and_half_gb, WHITELIST_GB_PRICE_RUB) == Decimal("3.00")
