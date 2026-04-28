from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from altlink.application.services.base import BaseService, ConflictError, NotFoundError
from altlink.infrastructure.db.models import PortalLoginAttempt, User
from altlink.utils.security import generate_token
from altlink.utils.time import ensure_utc, utc_now


class PortalAuthService(BaseService):
    source = "portal_auth"

    default_ttl_seconds = 600

    async def create_login_attempt(self, *, ttl_seconds: int | None = None) -> PortalLoginAttempt:
        lifetime = max(60, int(ttl_seconds or self.default_ttl_seconds))
        item = PortalLoginAttempt(
            token=generate_token(16),
            expires_at=utc_now() + timedelta(seconds=lifetime),
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def get_login_attempt(self, token: str) -> PortalLoginAttempt | None:
        normalized = self.normalize_token(token)
        if not normalized:
            return None
        return await self.session.scalar(
            select(PortalLoginAttempt)
            .options(joinedload(PortalLoginAttempt.approved_user))
            .where(PortalLoginAttempt.token == normalized)
        )

    def login_attempt_status(self, attempt: PortalLoginAttempt | None) -> str:
        if attempt is None:
            return "missing"
        now = utc_now()
        if attempt.consumed_at is not None:
            return "completed"
        if attempt.canceled_at is not None:
            return "canceled"
        if ensure_utc(attempt.expires_at) <= now:
            return "expired"
        if attempt.approved_user_id and attempt.approved_at is not None:
            return "approved"
        return "pending"

    async def approve_login_attempt(self, token: str, user_id: str) -> PortalLoginAttempt:
        attempt = await self.get_login_attempt(token)
        status = self.login_attempt_status(attempt)
        if attempt is None:
            raise NotFoundError("Попытка входа не найдена.")
        if status == "expired":
            raise ConflictError("Эта попытка входа уже истекла. Вернитесь на сайт и начните вход заново.")
        if status == "canceled":
            raise ConflictError("Эта попытка входа уже отменена. Вернитесь на сайт и начните вход заново.")
        if status == "completed":
            raise ConflictError("Эта попытка входа уже использована. Вернитесь на сайт и начните вход заново.")
        if status == "approved" and attempt.approved_user_id == user_id:
            return attempt
        if attempt.approved_user_id and attempt.approved_user_id != user_id:
            raise ConflictError("Эта попытка входа уже подтверждена другим Telegram-аккаунтом.")

        user = await self.session.get(User, user_id)
        if user is None:
            raise NotFoundError("Пользователь не найден.")

        attempt.approved_user_id = user.id
        attempt.approved_telegram_id = user.telegram_id
        attempt.approved_at = utc_now()
        attempt.canceled_at = None
        await self.session.flush()
        return attempt

    async def cancel_login_attempt(self, token: str) -> PortalLoginAttempt:
        attempt = await self.get_login_attempt(token)
        status = self.login_attempt_status(attempt)
        if attempt is None:
            raise NotFoundError("Попытка входа не найдена.")
        if status == "completed":
            raise ConflictError("Эта попытка входа уже завершена.")
        if status == "expired":
            raise ConflictError("Эта попытка входа уже истекла.")
        if attempt.canceled_at is None:
            attempt.canceled_at = utc_now()
            await self.session.flush()
        return attempt

    async def consume_login_attempt(self, token: str) -> User:
        attempt = await self.get_login_attempt(token)
        status = self.login_attempt_status(attempt)
        if attempt is None:
            raise NotFoundError("Попытка входа не найдена.")
        if status == "pending":
            raise ConflictError("Вход через Telegram ещё не подтверждён.")
        if status == "expired":
            raise ConflictError("Эта попытка входа уже истекла.")
        if status == "canceled":
            raise ConflictError("Эта попытка входа была отменена.")
        if attempt.approved_user_id is None:
            raise ConflictError("Не удалось определить пользователя для входа.")

        user = await self.session.get(User, attempt.approved_user_id)
        if user is None:
            raise NotFoundError("Пользователь для входа не найден.")

        if attempt.consumed_at is None:
            attempt.consumed_at = utc_now()
            await self.session.flush()
        return user

    @staticmethod
    def normalize_token(token: str | None) -> str:
        return (token or "").strip()
