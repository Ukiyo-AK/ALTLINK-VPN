from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select

from altlink.application.services.base import ServiceBase
from altlink.core.security import hash_password, verify_password
from altlink.domain.enums import EventLevel
from altlink.infrastructure.db.models import AdminUser


class AdminAuthService(ServiceBase):
    async def authenticate(self, username: str, password: str) -> AdminUser | None:
        result = await self.session.execute(
            select(AdminUser).where(AdminUser.username == username, AdminUser.is_active.is_(True))
        )
        admin = result.scalar_one_or_none()
        if admin is None or not verify_password(password, admin.password_hash):
            return None
        admin.last_login_at = datetime.now(UTC)
        await self.log_event(
            scope="admin_auth",
            level=EventLevel.INFO,
            title="Успешный вход администратора",
            details=f"username={username}",
        )
        return admin

    async def create_or_update_admin(
        self,
        *,
        username: str,
        password: str,
        telegram_id: int | None = None,
        full_name: str | None = None,
    ) -> AdminUser:
        result = await self.session.execute(
            select(AdminUser).where(
                or_(AdminUser.username == username, AdminUser.telegram_id == telegram_id)
            )
        )
        admin = result.scalar_one_or_none()
        if admin is None:
            admin = AdminUser(
                username=username,
                password_hash=hash_password(password),
                telegram_id=telegram_id,
                full_name=full_name,
            )
            self.session.add(admin)
        else:
            admin.password_hash = hash_password(password)
            admin.telegram_id = telegram_id
            admin.full_name = full_name
            admin.is_active = True
        await self.session.flush()
        await self.log_event(
            scope="admin_auth",
            level=EventLevel.INFO,
            title="Администратор создан или обновлен",
            details=f"username={username}",
        )
        return admin

    async def get_admin_by_telegram_id(self, telegram_id: int) -> AdminUser | None:
        result = await self.session.execute(
            select(AdminUser).where(AdminUser.telegram_id == telegram_id, AdminUser.is_active.is_(True))
        )
        return result.scalar_one_or_none()

    async def is_admin_telegram_id(self, telegram_id: int) -> bool:
        if telegram_id in self.settings.admin_telegram_ids:
            return True
        admin = await self.get_admin_by_telegram_id(telegram_id)
        return admin is not None
