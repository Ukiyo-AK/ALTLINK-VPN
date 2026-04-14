from __future__ import annotations

from altlink.domain.billing import detect_threshold_crossing


def test_detect_threshold_crossing_returns_highest_new_marker():
    threshold = detect_threshold_crossing(
        used_bytes=46,
        limit_bytes=50,
        thresholds=[70, 90, 100],
        last_threshold=70,
    )
    assert threshold == 90

