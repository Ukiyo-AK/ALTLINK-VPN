from __future__ import annotations

from datetime import datetime

from aiogram import Bot
from sqlalchemy import Select, select

from altlink.application.services.base import BaseService
from altlink.domain.enums import NotificationStatus, NotificationType, SystemEventLevel
from altlink.infrastructure.db.models import Notification, User


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
            return 0

        query = await self.pending_query()
        notifications = list((await self.session.scalars(query)).all())
        if not notifications:
            return 0

        delivered = 0
        bot = Bot(token=bot_token)
        try:
            for item in notifications:
                user = await self.session.get(User, item.user_id)
                if user is None:
                    item.status = NotificationStatus.FAILED
                    item.failure_reason = "User not found"
                    continue
                try:
                    await bot.send_message(chat_id=user.telegram_id, text=item.message)
                    item.status = NotificationStatus.SENT
                    item.sent_at = datetime.now().astimezone()
                    delivered += 1
                except Exception as exc:  # pragma: no cover - network side effect
                    item.status = NotificationStatus.FAILED
                    item.failure_reason = str(exc)
                    await self.log_event(
                        level=SystemEventLevel.ERROR,
                        event_type="notification_failed",
                        message="Не удалось доставить уведомление в Telegram.",
                        payload={"notification_id": item.id, "error": str(exc)},
                    )
        finally:
            await bot.session.close()
        return delivered

