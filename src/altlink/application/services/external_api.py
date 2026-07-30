from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import joinedload

from altlink.application.services.base import AuthError, BaseService, ConflictError, NotFoundError
from altlink.domain.enums import PlanCode, SubscriptionStatus, SystemEventLevel, UserStatus
from altlink.domain.external_api import EXTERNAL_API_SCOPES, normalize_external_api_scopes
from altlink.infrastructure.db.models import ExternalApiClient, Plan, Subscription, User
from altlink.utils.security import generate_token
from altlink.utils.time import ensure_utc, utc_now


@dataclass(frozen=True, slots=True)
class IssuedExternalApiKey:
    client: ExternalApiClient
    api_key: str


@dataclass(frozen=True, slots=True)
class ExternalApiUserPage:
    users: list[User]
    subscriptions_by_user_id: dict[str, Subscription]
    has_more: bool


class ExternalApiService(BaseService):
    source = "external_api"
    key_prefix_marker = "altlink"
    active_user_statuses = (UserStatus.ACTIVE, UserStatus.TRIAL, UserStatus.GRACE)

    @staticmethod
    def hash_api_key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    @classmethod
    def parse_key_prefix(cls, api_key: str) -> str | None:
        parts = (api_key or "").strip().split("_", 2)
        if len(parts) != 3 or parts[0] != cls.key_prefix_marker:
            return None
        return parts[1] or None

    async def list_clients(self) -> list[ExternalApiClient]:
        return list(
            (
                await self.session.scalars(
                    select(ExternalApiClient).order_by(ExternalApiClient.created_at.desc())
                )
            ).all()
        )

    async def get_client(self, client_id: str) -> ExternalApiClient:
        client = await self.session.get(ExternalApiClient, client_id)
        if client is None:
            raise NotFoundError("API-клиент не найден.")
        return client

    async def create_client(
        self,
        *,
        name: str,
        description: str | None,
        scopes: list[str],
        expires_at: datetime | None,
        admin_id: str | None,
    ) -> IssuedExternalApiKey:
        normalized_name = (name or "").strip()
        if not normalized_name:
            raise ConflictError("Укажите название API-клиента.")
        normalized_scopes = self._validate_scopes(scopes)
        normalized_expires_at = ensure_utc(expires_at) if expires_at is not None else None
        if normalized_expires_at is not None and normalized_expires_at <= utc_now():
            raise ConflictError("Срок действия API-ключа должен быть в будущем.")

        api_key, key_prefix = await self._issue_unique_key()
        client = ExternalApiClient(
            name=normalized_name[:128],
            description=(description or "").strip()[:4000] or None,
            key_prefix=key_prefix,
            key_hash=self.hash_api_key(api_key),
            scopes=normalized_scopes,
            is_active=True,
            expires_at=normalized_expires_at,
            created_by_admin_id=admin_id,
        )
        self.session.add(client)
        await self.session.flush()
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="external_api_client_created",
            message="Создан внешний API-клиент.",
            payload={
                "api_client_id": client.id,
                "name": client.name,
                "key_prefix": client.key_prefix,
                "scopes": client.scopes,
            },
            actor_admin_id=admin_id,
        )
        return IssuedExternalApiKey(client=client, api_key=api_key)

    async def rotate_key(
        self,
        client_id: str,
        *,
        admin_id: str | None,
    ) -> IssuedExternalApiKey:
        client = await self.get_client(client_id)
        if client.revoked_at is not None:
            raise ConflictError("Нельзя перевыпустить ключ отозванного API-клиента.")
        api_key, key_prefix = await self._issue_unique_key()
        client.key_prefix = key_prefix
        client.key_hash = self.hash_api_key(api_key)
        client.is_active = True
        await self.log_event(
            level=SystemEventLevel.WARNING,
            event_type="external_api_key_rotated",
            message="Перевыпущен ключ внешнего API-клиента.",
            payload={
                "api_client_id": client.id,
                "name": client.name,
                "key_prefix": client.key_prefix,
            },
            actor_admin_id=admin_id,
        )
        await self.session.flush()
        return IssuedExternalApiKey(client=client, api_key=api_key)

    async def set_active(
        self,
        client_id: str,
        *,
        is_active: bool,
        admin_id: str | None,
    ) -> ExternalApiClient:
        client = await self.get_client(client_id)
        if client.revoked_at is not None:
            raise ConflictError("Отозванный API-клиент нельзя включить повторно.")
        client.is_active = is_active
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="external_api_client_toggled",
            message="Изменён статус внешнего API-клиента.",
            payload={
                "api_client_id": client.id,
                "name": client.name,
                "is_active": client.is_active,
            },
            actor_admin_id=admin_id,
        )
        return client

    async def update_scopes(
        self,
        client_id: str,
        *,
        scopes: list[str],
        admin_id: str | None,
    ) -> ExternalApiClient:
        client = await self.get_client(client_id)
        if client.revoked_at is not None:
            raise ConflictError("Нельзя изменить разрешения отозванного API-клиента.")
        client.scopes = self._validate_scopes(scopes)
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="external_api_client_scopes_updated",
            message="Изменены разрешения внешнего API-клиента.",
            payload={
                "api_client_id": client.id,
                "name": client.name,
                "scopes": client.scopes,
            },
            actor_admin_id=admin_id,
        )
        return client

    async def revoke_client(
        self,
        client_id: str,
        *,
        admin_id: str | None,
    ) -> ExternalApiClient:
        client = await self.get_client(client_id)
        if client.revoked_at is None:
            client.revoked_at = utc_now()
            client.is_active = False
            await self.log_event(
                level=SystemEventLevel.WARNING,
                event_type="external_api_client_revoked",
                message="Отозван внешний API-клиент.",
                payload={"api_client_id": client.id, "name": client.name},
                actor_admin_id=admin_id,
            )
        return client

    async def authenticate(self, api_key: str, *, source_ip: str | None = None) -> ExternalApiClient:
        key_prefix = self.parse_key_prefix(api_key)
        if key_prefix is None:
            raise AuthError("Неверный API-ключ.")
        client = await self.session.scalar(
            select(ExternalApiClient).where(ExternalApiClient.key_prefix == key_prefix)
        )
        if client is None or not hmac.compare_digest(
            client.key_hash,
            self.hash_api_key(api_key.strip()),
        ):
            raise AuthError("Неверный API-ключ.")
        now = utc_now()
        if not client.is_active or client.revoked_at is not None:
            raise AuthError("API-ключ отключён или отозван.")
        if client.expires_at is not None and ensure_utc(client.expires_at) <= now:
            raise AuthError("Срок действия API-ключа истёк.")

        client.last_used_at = now
        client.last_used_ip = (source_ip or "").strip()[:128] or None
        client.request_count = int(client.request_count or 0) + 1
        await self.session.flush()
        return client

    async def list_users_for_api(
        self,
        *,
        active_only: bool,
        plan_code: PlanCode | None,
        limit: int,
        offset: int,
    ) -> ExternalApiUserPage:
        now = utc_now()
        active_access = self._active_access_condition(now)
        query = select(User)
        if active_only:
            query = query.where(
                User.status.in_(self.active_user_statuses),
                select(Subscription.id)
                .where(Subscription.user_id == User.id, active_access)
                .exists()
            )
        if plan_code is not None:
            query = query.where(
                select(Subscription.id)
                .join(Plan, Plan.id == Subscription.plan_id)
                .where(
                    Subscription.user_id == User.id,
                    active_access,
                    Plan.code == plan_code,
                )
                .exists()
            )
        records = list(
            (
                await self.session.scalars(
                    query.order_by(User.created_at.desc(), User.id.desc())
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
        )
        has_more = len(records) > limit
        users = records[:limit]
        subscriptions_by_user_id = await self._active_subscriptions_for_users(
            [user.id for user in users],
            now=now,
        )
        return ExternalApiUserPage(
            users=users,
            subscriptions_by_user_id=subscriptions_by_user_id,
            has_more=has_more,
        )

    async def get_user_for_api(self, user_id: str) -> tuple[User, Subscription | None]:
        user = await self.session.get(User, user_id)
        if user is None:
            raise NotFoundError("Пользователь не найден.")
        subscriptions = await self._active_subscriptions_for_users([user.id], now=utc_now())
        subscription = subscriptions.get(user.id)
        if user.status not in self.active_user_statuses:
            subscription = None
        return user, subscription

    async def get_user_by_telegram_id_for_api(
        self,
        telegram_id: int,
    ) -> tuple[User, Subscription | None]:
        user = await self.session.scalar(
            select(User).where(User.telegram_id == telegram_id)
        )
        if user is None:
            raise NotFoundError("Пользователь не найден.")
        subscriptions = await self._active_subscriptions_for_users([user.id], now=utc_now())
        subscription = subscriptions.get(user.id)
        if user.status not in self.active_user_statuses:
            subscription = None
        return user, subscription

    @staticmethod
    def _active_access_condition(now: datetime):
        return or_(
            and_(
                Subscription.status.in_(
                    [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL]
                ),
                Subscription.ends_at > now,
            ),
            and_(
                Subscription.status == SubscriptionStatus.GRACE,
                func.coalesce(Subscription.grace_until, Subscription.ends_at) > now,
            ),
        )

    async def _active_subscriptions_for_users(
        self,
        user_ids: list[str],
        *,
        now: datetime,
    ) -> dict[str, Subscription]:
        if not user_ids:
            return {}
        subscriptions = list(
            (
                await self.session.scalars(
                    select(Subscription)
                    .where(
                        Subscription.user_id.in_(user_ids),
                        self._active_access_condition(now),
                    )
                    .options(joinedload(Subscription.plan))
                    .order_by(Subscription.created_at.desc())
                )
            ).all()
        )
        result: dict[str, Subscription] = {}
        for subscription in subscriptions:
            result.setdefault(subscription.user_id, subscription)
        return result

    def _validate_scopes(self, scopes: list[str]) -> list[str]:
        normalized = normalize_external_api_scopes(scopes)
        if not normalized:
            raise ConflictError("Выберите хотя бы одно разрешение API.")
        unknown = sorted(set(normalized) - EXTERNAL_API_SCOPES)
        if unknown:
            raise ConflictError(f"Неизвестные разрешения API: {', '.join(unknown)}.")
        return normalized

    async def _issue_unique_key(self) -> tuple[str, str]:
        for _ in range(10):
            key_prefix = secrets.token_hex(5)
            exists = await self.session.scalar(
                select(ExternalApiClient.id).where(
                    ExternalApiClient.key_prefix == key_prefix
                )
            )
            if exists is not None:
                continue
            secret = generate_token(32)
            return f"{self.key_prefix_marker}_{key_prefix}_{secret}", key_prefix
        raise ConflictError("Не удалось создать уникальный API-ключ. Повторите попытку.")
