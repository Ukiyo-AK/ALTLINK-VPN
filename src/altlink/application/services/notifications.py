from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import Select, select

from altlink.application.services.base import BaseService
from altlink.domain.enums import NotificationStatus, NotificationType, SystemEventLevel
from altlink.infrastructure.db.models import Notification, User

logger = logging.getLogger(__name__)


class NotificationService(BaseService):
    source = "notifications"

    async def queue(
        self,
        *,
        user_id: str,
        notification_type: NotificationType,
        message: str,
        payload: dict | None = None,
        dedupe_key: str | None = None,
    ) -> Notification:
        if dedupe_key:
            existing = await self.session.scalar(
                select(Notification).where(Notification.dedupe_key == dedupe_key)
            )
            if existing:
                return existing

        notification = Notification(
            user_id=user_id,
            type=notification_type,
            message=message,
            payload=payload,
            dedupe_key=dedupe_key,
        )
        self.session.add(notification)
        await self.session.flush()
        return notification

    async def pending_query(self, limit: int = 100) -> Select[tuple[Notification]]:
        return (
            select(Notification)
            .where(Notification.status == NotificationStatus.PENDING)
            .order_by(Notification.created_at.asc())
            .limit(limit)
        )

    async def dispatch_pending(self, bot_token: str) -> int:
        if not bot_token:
            logger.warning("Notification dispatch skipped: client bot token is not configured")
            return 0

        rows = list(
            (
                await self.session.execute(
                    select(
                        Notification.id,
                        Notification.user_id,
                        Notification.type,
                        Notification.message,
                        Notification.payload,
                        Notification.dedupe_key,
                        User.telegram_id,
                    )
                    .select_from(Notification)
                    .join(User, User.id == Notification.user_id, isouter=True)
                    .where(Notification.status == NotificationStatus.PENDING)
                    .order_by(Notification.created_at.asc())
                    .limit(100)
                )
            ).all()
        )
        if not rows:
            return 0

        logger.info("Notification dispatch started: %s pending item(s)", len(rows))

        # Release the DB transaction before any network I/O to Telegram.
        await self.session.commit()

        delivered = 0
        failed = 0
        outcomes: dict[str, dict[str, object | None]] = {}
        bot = Bot(token=bot_token)
        try:
            for notification_id, user_id, notification_type, message, payload, dedupe_key, telegram_id in rows:
                if telegram_id is None:
                    failed += 1
                    outcomes[notification_id] = {
                        "status": NotificationStatus.FAILED,
                        "failure_reason": "User not found",
                        "sent_at": None,
                    }
                    logger.warning(
                        "Notification failed before send: id=%s type=%s user_id=%s reason=%s",
                        notification_id,
                        notification_type,
                        user_id,
                        "User not found",
                    )
                    continue

                try:
                    parse_mode = self._notification_parse_mode(notification_type, payload)
                    reply_markup = self._notification_reply_markup(payload)
                    await bot.send_message(
                        chat_id=telegram_id,
                        text=message,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                    )
                    outcomes[notification_id] = {
                        "status": NotificationStatus.SENT,
                        "failure_reason": None,
                        "sent_at": datetime.now().astimezone(),
                    }
                    delivered += 1
                    logger.info(
                        "Notification sent: id=%s type=%s user_id=%s telegram_id=%s dedupe_key=%s",
                        notification_id,
                        notification_type,
                        user_id,
                        telegram_id,
                        dedupe_key,
                    )
                except Exception as exc:  # pragma: no cover - network side effect
                    failed += 1
                    outcomes[notification_id] = {
                        "status": NotificationStatus.FAILED,
                        "failure_reason": str(exc),
                        "sent_at": None,
                    }
                    logger.exception(
                        "Notification send failed: id=%s type=%s user_id=%s telegram_id=%s",
                        notification_id,
                        notification_type,
                        user_id,
                        telegram_id,
                    )
        finally:
            await bot.session.close()

        notifications = list(
            (
                await self.session.scalars(
                    select(Notification).where(Notification.id.in_(list(outcomes.keys())))
                )
            ).all()
        )
        for item in notifications:
            outcome = outcomes.get(item.id)
            if outcome is None:
                continue
            item.status = outcome["status"]  # type: ignore[assignment]
            item.failure_reason = outcome["failure_reason"]  # type: ignore[assignment]
            item.sent_at = outcome["sent_at"]  # type: ignore[assignment]
            if outcome["status"] == NotificationStatus.FAILED:
                await self.log_event(
                    level=SystemEventLevel.ERROR,
                    event_type="notification_failed",
                    message="Не удалось доставить уведомление в Telegram.",
                    payload={"notification_id": item.id, "error": outcome["failure_reason"]},
                )

        await self.session.flush()
        logger.info(
            "Notification dispatch finished: sent=%s failed=%s total=%s",
            delivered,
            failed,
            len(rows),
        )
        return delivered

    @staticmethod
    def _notification_parse_mode(notification_type: NotificationType, payload: dict | None) -> str | None:
        if isinstance(payload, dict):
            parse_mode = payload.get("parse_mode")
            if isinstance(parse_mode, str) and parse_mode:
                return parse_mode
        if notification_type == NotificationType.PROMO_CODE:
            return "HTML"
        return None

    @staticmethod
    def _notification_reply_markup(payload: dict | None) -> InlineKeyboardMarkup | None:
        if not isinstance(payload, dict):
            return None

        cta = payload.get("cta")
        if cta == "trial_expiring":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🧾 Подписка", callback_data="client:subscription")],
                ]
            )
        if cta == "trial_ended":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🧾 Выбрать тариф", callback_data="client:plan_menu")],
                    [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="client:topup_menu")],
                ]
            )
        if cta == "return_trial":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🎁 Активировать 2 дня бесплатно", callback_data="client:trial_activate")],
                ]
            )
        if cta == "renewal_disabled_expiring":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔄 Включить автопродление",
                            callback_data="client:subscription_resume",
                        )
                    ],
                    [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="client:topup_menu")],
                ]
            )
        if cta == "low_balance":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="client:topup_menu")],
                ]
            )
        if cta == "access_blocked":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="client:topup_menu")],
                    [InlineKeyboardButton(text="🧾 Подписка", callback_data="client:subscription")],
                ]
            )
        if cta in {"inactive_promo", "trial_followup"}:
            promo_code = payload.get("promo_code")
            if not isinstance(promo_code, str) or not promo_code or len(promo_code) > 40:
                return None
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🎟 Использовать промокод",
                            callback_data=f"client:promo_apply:{promo_code}",
                        )
                    ],
                ]
            )
        if cta == "trial_setup_help":
            support_url = payload.get("support_url")
            if isinstance(support_url, str) and support_url:
                return InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="💬 Написать в поддержку", url=support_url)],
                    ]
                )
        return None
