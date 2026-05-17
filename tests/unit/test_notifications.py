from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from altlink.domain.enums import NotificationStatus, NotificationType
from altlink.infrastructure.db.models import Notification
from altlink.domain.notifications import (
    grace_started_message,
    inactive_subscription_promo_message,
    low_balance_message,
    trial_expiring_message,
)


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


def test_trial_expiring_message_mentions_window_deadline_and_emoji():
    message = trial_expiring_message(
        datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
        "24 часа",
    )
    assert "⏳" in message
    assert "24 часа" in message
    assert "15.01.2026" in message


def test_inactive_subscription_promo_message_contains_code_discount_and_emoji():
    message = inactive_subscription_promo_message()
    assert "🎁" in message
    assert "ALT10" in message
    assert "10%" in message


@pytest.mark.asyncio
async def test_dispatch_pending_sends_notifications_and_marks_them_sent(test_services, monkeypatch):
    sent_messages: list[tuple[int, str]] = []

    class DummyBotSession:
        async def close(self):
            return None

    class DummyBot:
        def __init__(self, token: str):
            self.token = token
            self.session = DummyBotSession()

        async def send_message(self, chat_id: int, text: str):
            sent_messages.append((chat_id, text))

    monkeypatch.setattr("altlink.application.services.notifications.Bot", DummyBot)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=19001,
            username="notify_user",
            first_name="Notify",
            last_name="User",
            language_code="ru",
        )
        notification = await hub.notifications.queue(
            user_id=user.id,
            notification_type=NotificationType.BROADCAST,
            message="Test notification",
            dedupe_key="test-notification-dispatch",
        )

        delivered = await hub.notifications.dispatch_pending("client-token")
        refreshed = await hub.session.scalar(select(Notification).where(Notification.id == notification.id))

    assert delivered == 1
    assert sent_messages == [(19001, "Test notification")]
    assert refreshed is not None
    assert refreshed.status == NotificationStatus.SENT
