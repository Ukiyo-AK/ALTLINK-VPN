from __future__ import annotations

from altlink.domain.policies import reached_thresholds


def test_notification_thresholds_detected_correctly():
    assert reached_thresholds(45, 50, [70, 90, 100]) == [70, 90]


def test_notification_thresholds_empty_for_unlimited():
    assert reached_thresholds(1_000, None, [70, 90, 100]) == []

