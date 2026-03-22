from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Bot
from sqlalchemy import select

from altlink.application.services.base import ServiceBase
from altlink.domain.enums import EventLevel, NotificationStatus, NotificationType
from altlink.infrastructure.db.models import Notification, User


class NotificationService(ServiceBase):
    async def queue(
        self,
        *,
        user: User,
        notification_type: NotificationType,
        title: str,
        message: str,
        dedupe_key: str | None = None,
        scheduled_for: datetime | None = None,
        payload: dict | None = None,
    ) -> Notification:
        if dedupe_key:
            existing = await self.session.execute(
                select(Notification).where(Notification.dedupe_key == dedupe_key)
            )
            found = existing.scalar_one_or_none()
            if found is not None:
                return found
        notification = Notification(
            user_id=user.id,
            notification_type=notification_type,
            title=title,
            message=message,
            dedupe_key=dedupe_key,
            scheduled_for=scheduled_for,
            payload=payload,
        )
        self.session.add(notification)
        await self.session.flush()
        return notification

    async def send_due_notifications(self) -> int:
        bot = Bot(self.settings.client_bot_token)
        sent_count = 0
        now = datetime.now(UTC)
        result = await self.session.execute(
            select(Notification, User)
            .join(User, User.id == Notification.user_id)
            .where(
                Notification.status == NotificationStatus.PENDING,
                (Notification.scheduled_for.is_(None) | (Notification.scheduled_for <= now)),
            )
            .order_by(Notification.created_at.asc())
        )
        records = result.all()
        for notification, user in records:
            try:
                await bot.send_message(chat_id=user.telegram_id, text=notification.message)
                notification.status = NotificationStatus.SENT
                notification.sent_at = now
                sent_count += 1
            except Exception as exc:  # noqa: BLE001
                notification.status = NotificationStatus.FAILED
                notification.error_message = str(exc)
                await self.log_event(
                    scope="notifications",
                    level=EventLevel.ERROR,
                    title="Не удалось отправить уведомление",
                    details=str(exc),
                    user_id=user.id,
                )
        await bot.session.close()
        return sent_count

