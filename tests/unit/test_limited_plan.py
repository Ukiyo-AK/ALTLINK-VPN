from __future__ import annotations

from altlink.domain.policies import should_block_on_traffic_limit, traffic_percent


def test_limited_plan_traffic_percent():
    assert round(traffic_percent(35, 50), 2) == 70.0


def test_limited_plan_blocks_when_limit_reached():
    assert should_block_on_traffic_limit(50, 50) is True


def test_unlimited_plan_never_blocks():
    assert should_block_on_traffic_limit(10_000, None) is False

