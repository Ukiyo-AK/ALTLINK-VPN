from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from altlink.domain.notifications import grace_started_message, low_balance_message


def test_low_balance_message_contains_amounts_date_and_emoji():
    message = low_balance_message(
        Decimal("12"),
        Decimal("100"),
        datetime(2026, 1, 1, tzinfo=UTC),
        "меньше 3 дней",
    )
    assert "⚠️" in message
    assert "12.00" in message
    assert "100.00" in message
    assert "01.01.2026" in message
    assert "меньше 3 дней" in message


def test_grace_started_message_mentions_deadline_and_emoji():
    message = grace_started_message(
        Decimal("0"),
        Decimal("200"),
        datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
    )
    assert "⏳" in message
    assert "200.00" in message
    assert "15.01.2026" in message
