from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from altlink.application.services.notifications import NotificationService
from altlink.domain.enums import NotificationStatus, NotificationType
from altlink.infrastructure.db.models import Notification
from altlink.domain.notifications import (
    blocked_message,
    grace_started_message,
    inactive_subscription_promo_message,
    low_balance_message,
    renewal_disabled_expiring_message,
    trial_followup_message,
    trial_expiring_message,
    trial_setup_help_message,
    upcoming_renewal_message,
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


def test_upcoming_renewal_message_contains_current_balance_charge_and_date():
    message = upcoming_renewal_message(
        Decimal("25"),
        Decimal("69"),
        datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
    )

    assert "Текущий баланс: 25.00 ₽" in message
    assert "К списанию: 69.00 ₽" in message
    assert "05.07.2026 12:00" in message


def test_grace_started_message_mentions_deadline_and_emoji():
    message = grace_started_message(
        Decimal("0"),
        Decimal("200"),
        datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
    )
    assert "⏳" in message
    assert "200.00" in message
    assert "15.01.2026" in message


def test_blocked_message_uses_subscription_wording_by_default():
    message = blocked_message()
    assert "Подписка не была продлена" in message
    assert "Льготный период закончился" not in message


def test_blocked_message_can_explain_finished_grace_period():
    message = blocked_message(grace_ended=True)
    assert "Льготный период закончился" in message


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
    assert "⚡" in message
    assert "<code>ALT10</code>" in message
    assert "10%" in message


def test_trial_followup_message_contains_copyable_code_and_emoji():
    message = trial_followup_message()
    assert "😎" in message
    assert "<code>ALT10</code>" in message
    assert "10%" in message


def test_trial_setup_help_message_points_to_support():
    message = trial_setup_help_message("@altlink_support")
    assert "👋" in message
    assert "пробный период" in message
    assert "@altlink_support" in message
    assert "поддерж" in message.casefold()


def test_renewal_disabled_expiring_message_contains_balance_charge_and_deadline():
    message = renewal_disabled_expiring_message(
        Decimal("50"),
        Decimal("69"),
        datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
    )

    assert "Автопродление сейчас отключено" in message
    assert "50.00 ₽" in message
    assert "69.00 ₽" in message
    assert "05.07.2026 12:00" in message
    assert "не потерять доступ" in message


def test_renewal_disabled_notification_has_resume_and_topup_buttons():
    markup = NotificationService._notification_reply_markup(
        {"cta": "renewal_disabled_expiring"}
    )

    assert markup is not None
    assert [[button.callback_data for button in row] for row in markup.inline_keyboard] == [
        ["client:subscription_resume"],
        ["client:topup_menu"],
    ]


def test_return_trial_notification_has_trial_button():
    markup = NotificationService._notification_reply_markup({"cta": "return_trial"})

    assert markup is not None
    assert [[button.callback_data for button in row] for row in markup.inline_keyboard] == [
        ["client:trial_activate"],
    ]


@pytest.mark.asyncio
async def test_dispatch_pending_sends_notifications_and_marks_them_sent(test_services, monkeypatch):
    sent_messages: list[tuple[int, str, str | None, object | None]] = []

    class DummyBotSession:
        async def close(self):
            return None

    class DummyBot:
        def __init__(self, token: str):
            self.token = token
            self.session = DummyBotSession()

        async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None, reply_markup=None):
            sent_messages.append((chat_id, text, parse_mode, reply_markup))

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
    assert sent_messages == [(19001, "Test notification", None, None)]
    assert refreshed is not None
    assert refreshed.status == NotificationStatus.SENT


@pytest.mark.asyncio
async def test_dispatch_pending_sends_promo_notifications_with_html_parse_mode(test_services, monkeypatch):
    sent_messages: list[tuple[int, str, str | None, object | None]] = []

    class DummyBotSession:
        async def close(self):
            return None

    class DummyBot:
        def __init__(self, token: str):
            self.token = token
            self.session = DummyBotSession()

        async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None, reply_markup=None):
            sent_messages.append((chat_id, text, parse_mode, reply_markup))

    monkeypatch.setattr("altlink.application.services.notifications.Bot", DummyBot)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=19002,
            username="promo_notify_user",
            first_name="Promo",
            last_name="Notify",
            language_code="ru",
        )
        notification = await hub.notifications.queue(
            user_id=user.id,
            notification_type=NotificationType.PROMO_CODE,
            message=inactive_subscription_promo_message("ALT10-PERSONAL"),
            payload={
                "cta": "inactive_promo",
                "parse_mode": "HTML",
                "promo_code": "ALT10-PERSONAL",
            },
            dedupe_key="test-promo-notification-dispatch",
        )

        delivered = await hub.notifications.dispatch_pending("client-token")
        refreshed = await hub.session.scalar(select(Notification).where(Notification.id == notification.id))

    assert delivered == 1
    assert sent_messages[0][0] == 19002
    assert sent_messages[0][1] == inactive_subscription_promo_message("ALT10-PERSONAL")
    assert sent_messages[0][2] == "HTML"
    markup = sent_messages[0][3]
    assert markup is not None
    assert [[button.text for button in row] for row in markup.inline_keyboard] == [
        ["🎟 Использовать промокод"]
    ]
    assert [[button.callback_data for button in row] for row in markup.inline_keyboard] == [
        ["client:promo_apply:ALT10-PERSONAL"]
    ]
    assert refreshed is not None
    assert refreshed.status == NotificationStatus.SENT


@pytest.mark.asyncio
async def test_dispatch_pending_sends_trial_ended_notification_with_cta_buttons(test_services, monkeypatch):
    sent_messages: list[tuple[int, str, str | None, object | None]] = []

    class DummyBotSession:
        async def close(self):
            return None

    class DummyBot:
        def __init__(self, token: str):
            self.token = token
            self.session = DummyBotSession()

        async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None, reply_markup=None):
            sent_messages.append((chat_id, text, parse_mode, reply_markup))

    monkeypatch.setattr("altlink.application.services.notifications.Bot", DummyBot)

    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=19003,
            username="trial_notify_user",
            first_name="Trial",
            last_name="Notify",
            language_code="ru",
        )
        notification = await hub.notifications.queue(
            user_id=user.id,
            notification_type=NotificationType.TRIAL_ENDED,
            message="Trial ended",
            payload={"cta": "trial_ended"},
            dedupe_key="test-trial-ended-notification-dispatch",
        )

        delivered = await hub.notifications.dispatch_pending("client-token")
        refreshed = await hub.session.scalar(select(Notification).where(Notification.id == notification.id))

    assert delivered == 1
    assert sent_messages[0][0] == 19003
    assert sent_messages[0][1] == "Trial ended"
    assert sent_messages[0][2] is None
    markup = sent_messages[0][3]
    assert markup is not None
    callback_rows = [[button.callback_data for button in row] for row in markup.inline_keyboard]
    assert callback_rows == [["client:plan_menu"], ["client:topup_menu"]]
    assert refreshed is not None
    assert refreshed.status == NotificationStatus.SENT
