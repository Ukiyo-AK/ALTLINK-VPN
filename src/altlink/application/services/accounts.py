from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import httpx
from sqlalchemy import Text, cast, delete, func, or_, select
from sqlalchemy.orm import joinedload, selectinload

from altlink.application.services.base import AuthError, BaseService, ConflictError, NotFoundError
from altlink.domain.enums import (
    BalanceTransactionType,
    NotificationType,
    PlanCode,
    SubscriptionStatus,
    SystemEventLevel,
    UserStatus,
)
from altlink.domain.notifications import topup_approved_message, topup_rejected_message
from altlink.infrastructure.db.models import (
    AdminUser,
    BalanceTransaction,
    Notification,
    Plan,
    Subscription,
    TopupRequest,
    TrialPeriod,
    User,
)
from altlink.infrastructure.remnawave_schemas import RemoteUser
from altlink.utils.subscriptions import remnawave_public_subscription_url
from altlink.utils.security import hash_password, verify_password
from altlink.utils.time import utc_now

logger = logging.getLogger(__name__)


class AccountService(BaseService):
    source = "accounts"
    registration_consent_version = "placeholder-v1"

    async def get_or_create_user(
        self,
        *,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        language_code: str | None,
    ) -> User:
        user = await self.session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                language_code=language_code,
                status=UserStatus.NEW,
            )
            self.session.add(user)
            await self.log_event(
                level=SystemEventLevel.INFO,
                event_type="user_registered",
                message="Зарегистрирован новый пользователь Telegram.",
                payload={"telegram_id": telegram_id},
            )
        else:
            user.username = username
            user.first_name = first_name
            user.last_name = last_name
            user.language_code = language_code

        if not user.referral_code:
            user.referral_code = await self._generate_unique_referral_code()

        user.last_seen_at = utc_now()
        await self.session.flush()
        return user

    async def get_user(self, user_id: str) -> User:
        user = await self.session.get(
            User,
            user_id,
            options=[
                selectinload(User.subscriptions).joinedload(Subscription.plan),
                selectinload(User.topup_requests),
                selectinload(User.balance_transactions),
                joinedload(User.assigned_server),
                joinedload(User.trial_period),
            ],
        )
        if user is None:
            raise NotFoundError("Пользователь не найден.")
        return user

    async def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        return await self.session.scalar(
            select(User).options(joinedload(User.assigned_server)).where(User.telegram_id == telegram_id)
        )

    async def get_user_by_remnawave_short_uuid(self, short_uuid: str) -> User | None:
        return await self.session.scalar(
            select(User).options(joinedload(User.assigned_server)).where(User.remnawave_short_uuid == short_uuid)
        )

    async def list_users(self, search: str | None = None) -> Sequence[User]:
        query = select(User).options(joinedload(User.assigned_server)).order_by(User.created_at.desc())
        if search:
            normalized = search.strip()
            if not normalized:
                return list((await self.session.scalars(query.limit(200))).all())
            username_search = normalized.removeprefix("@")
            exact_match = normalized.casefold()
            exact_conditions = [
                cast(User.telegram_id, Text) == normalized,
                func.lower(User.id) == exact_match,
                func.lower(func.coalesce(User.remnawave_user_uuid, "")) == exact_match,
                func.lower(func.coalesce(User.remnawave_short_uuid, "")) == exact_match,
            ]
            if username_search:
                username_exact = username_search.casefold()
                exact_conditions.extend(
                    [
                        func.lower(func.coalesce(User.username, "")) == username_exact,
                        func.lower(func.coalesce(User.remnawave_username, "")) == username_exact,
                    ]
                )
            exact_query = query.where(or_(*exact_conditions)).limit(20)
            exact_matches = list((await self.session.scalars(exact_query)).all())
            if exact_matches:
                return exact_matches

            search_like = f"%{username_search or normalized}%"
            raw_like = f"%{normalized}%"
            query = query.where(
                or_(
                    User.username.ilike(search_like),
                    User.first_name.ilike(search_like),
                    User.last_name.ilike(search_like),
                    User.remnawave_username.ilike(search_like),
                    cast(User.telegram_id, Text).ilike(raw_like),
                    User.id.ilike(raw_like),
                    User.remnawave_user_uuid.ilike(raw_like),
                    User.remnawave_short_uuid.ilike(raw_like),
                )
            )
        return list((await self.session.scalars(query.limit(200))).all())

    async def complete_registration(
        self,
        user_id: str,
        *,
        consent_version: str | None = None,
    ) -> User:
        user = await self.get_user(user_id)
        if self.is_registered(user) and (
            consent_version is None or user.consent_version == consent_version
        ):
            return user

        now = utc_now()
        user.registration_completed_at = user.registration_completed_at or now
        user.consent_accepted_at = now
        user.consent_version = consent_version or self.registration_consent_version
        await self.session.flush()
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="user_registration_completed",
            message="Пользователь завершил регистрацию и подтвердил согласие.",
            payload={
                "user_id": user.id,
                "telegram_id": user.telegram_id,
                "consent_version": user.consent_version,
            },
        )
        return user

    @staticmethod
    def is_registered(user: User) -> bool:
        return bool(user.registration_completed_at and user.consent_accepted_at)

    async def mark_channel_verified(self, user_id: str) -> User:
        user = await self.get_user(user_id)
        user.channel_verified_at = utc_now()
        await self.session.flush()
        return user

    async def mark_promo_onboarding_completed(self, user_id: str) -> User:
        user = await self.get_user(user_id)
        if user.promo_onboarding_completed_at is None:
            user.promo_onboarding_completed_at = utc_now()
            await self.session.flush()
        return user

    @staticmethod
    def has_completed_promo_onboarding(user: User) -> bool:
        return bool(getattr(user, "promo_onboarding_completed_at", None))

    async def is_trial_available(self, user_id: str) -> bool:
        trial = await self.session.scalar(select(TrialPeriod).where(TrialPeriod.user_id == user_id))
        return trial is None or not trial.consumed

    async def has_user_transactions(self, user_id: str) -> bool:
        balance_transaction_id = await self.session.scalar(
            select(BalanceTransaction.id).where(BalanceTransaction.user_id == user_id).limit(1)
        )
        if balance_transaction_id is not None:
            return True

        topup_request_id = await self.session.scalar(
            select(TopupRequest.id).where(TopupRequest.user_id == user_id).limit(1)
        )
        return topup_request_id is not None

    async def has_paid_subscription_history(self, user_id: str) -> bool:
        paid_subscription = await self.session.scalar(
            select(Subscription.id)
            .join(Plan, Subscription.plan_id == Plan.id)
            .where(Subscription.user_id == user_id, Plan.is_trial.is_(False))
            .limit(1)
        )
        return paid_subscription is not None

    async def is_new_account_for_promo(self, user_id: str) -> bool:
        if await self.has_user_transactions(user_id):
            return False
        return not await self.has_paid_subscription_history(user_id)

    async def can_offer_trial(self, user_id: str) -> bool:
        if not await self.is_trial_available(user_id):
            return False
        return not await self.has_user_transactions(user_id)

    async def get_current_subscription(self, user_id: str) -> Subscription | None:
        return await self.session.scalar(
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status.in_(
                    [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE]
                ),
            )
            .options(joinedload(Subscription.plan))
            .order_by(Subscription.created_at.desc())
        )

    async def get_latest_subscription(self, user_id: str) -> Subscription | None:
        return await self.session.scalar(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .options(joinedload(Subscription.plan))
            .order_by(Subscription.created_at.desc())
        )

    async def get_plan(self, plan_code: PlanCode) -> Plan:
        plan = await self.session.scalar(select(Plan).where(Plan.code == plan_code, Plan.is_active.is_(True)))
        if plan is None:
            raise NotFoundError("Тариф не найден.")
        return plan

    async def ensure_remote_user_link(self, user: User) -> User:
        if self.remnawave is None or user.remnawave_user_uuid:
            return user
        remote = await self.remnawave.find_user_by_telegram_id(user.telegram_id)
        if remote:
            self._apply_remote_user(user, remote)
            await self.session.flush()
        return user

    async def get_subscription_bundle(self, user_id: str) -> dict:
        user = await self.get_user(user_id)
        await self.ensure_remote_user_link(user)
        subscription = await self.get_current_subscription(user.id)
        if self.remnawave is None or not user.remnawave_user_uuid or not user.remnawave_short_uuid:
            return {"user": user, "subscription": subscription}

        bundle = {
            "user": user,
            "subscription": subscription,
        }

        async def safe_remote_part(label: str, loader, *, default):
            try:
                return await loader()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code == 404:
                    logger.warning(
                        "Remnawave bundle part %s is missing for user %s; continuing with partial bundle.",
                        label,
                        user.id,
                    )
                    return default
                logger.warning(
                    "Failed to load Remnawave bundle part %s for user %s.",
                    label,
                    user.id,
                    exc_info=True,
                )
                return default
            except httpx.HTTPError:
                logger.warning(
                    "Failed to load Remnawave bundle part %s for user %s.",
                    label,
                    user.id,
                    exc_info=True,
                )
                return default

        bundle["accessible_nodes"] = await safe_remote_part(
            "accessible_nodes",
            lambda: self.remnawave.get_accessible_nodes(user.remnawave_user_uuid),
            default=[],
        )
        bundle["connection_keys"] = await safe_remote_part(
            "connection_keys",
            lambda: self.remnawave.get_connection_keys(user.remnawave_user_uuid),
            default=None,
        )
        bundle["subscription_info"] = await safe_remote_part(
            "subscription_info",
            lambda: self.remnawave.get_subscription_info(user.remnawave_short_uuid),
            default=None,
        )
        bundle["subscription_url"] = (
            bundle["subscription_info"].subscriptionUrl
            if bundle["subscription_info"] is not None
            else remnawave_public_subscription_url(self.settings, user.remnawave_short_uuid)
        )

        return bundle

    async def adjust_balance(
        self,
        *,
        user_id: str,
        amount_rub: Decimal,
        transaction_type: BalanceTransactionType,
        description: str,
        admin_id: str | None = None,
        topup_request_id: str | None = None,
    ) -> BalanceTransaction:
        user = await self.get_user(user_id)
        before = Decimal(user.balance_rub)
        after = before + Decimal(amount_rub)
        user.balance_rub = after

        transaction = BalanceTransaction(
            user_id=user_id,
            topup_request_id=topup_request_id,
            created_by_admin_id=admin_id,
            type=transaction_type,
            amount_rub=Decimal(amount_rub),
            balance_before=before,
            balance_after=after,
            description=description,
        )
        self.session.add(transaction)
        await self.session.flush()
        return transaction

    async def create_admin(
        self,
        *,
        username: str,
        password: str,
        full_name: str | None,
        telegram_id: int | None,
    ) -> AdminUser:
        existing = await self.session.scalar(select(AdminUser).where(AdminUser.username == username))
        if existing:
            raise ConflictError("Администратор с таким логином уже существует.")
        admin = AdminUser(
            username=username,
            password_hash=hash_password(password),
            full_name=full_name,
            telegram_id=telegram_id,
        )
        self.session.add(admin)
        await self.session.flush()
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="admin_created",
            message="Создан новый администратор.",
            payload={"username": username, "telegram_id": telegram_id},
            actor_admin_id=admin.id,
        )
        return admin

    async def authenticate_admin(self, username: str, password: str) -> AdminUser:
        admin = await self.session.scalar(select(AdminUser).where(AdminUser.username == username))
        if admin is None or not admin.is_active or not verify_password(password, admin.password_hash):
            raise AuthError("Неверный логин или пароль.")
        admin.last_login_at = utc_now()
        return admin

    async def get_admin(self, admin_id: str) -> AdminUser:
        admin = await self.session.get(AdminUser, admin_id)
        if admin is None:
            raise NotFoundError("Администратор не найден.")
        return admin

    async def get_admin_by_telegram_id(self, telegram_id: int) -> AdminUser | None:
        return await self.session.scalar(
            select(AdminUser).where(AdminUser.telegram_id == telegram_id, AdminUser.is_active.is_(True))
        )

    async def can_access_admin_bot(self, telegram_id: int) -> bool:
        if telegram_id in self.settings.admin_allowed_telegram_ids:
            return True
        return await self.get_admin_by_telegram_id(telegram_id) is not None

    async def user_card(self, user_id: str) -> dict:
        user = await self.get_user(user_id)
        subscription = await self.get_current_subscription(user.id)
        topups = list(
            (
                await self.session.scalars(
                    select(TopupRequest)
                    .where(TopupRequest.user_id == user.id)
                    .order_by(TopupRequest.created_at.desc())
                    .limit(10)
                )
            ).all()
        )
        transactions = list(
            (
                await self.session.scalars(
                    select(BalanceTransaction)
                    .where(BalanceTransaction.user_id == user.id)
                    .order_by(BalanceTransaction.created_at.desc())
                    .limit(20)
                )
            ).all()
        )
        return {"user": user, "subscription": subscription, "topups": topups, "transactions": transactions}

    async def bind_referrer(self, user_id: str, referral_code: str | None) -> User:
        user = await self.get_user(user_id)
        normalized = (referral_code or "").strip().upper()
        if not normalized or user.referred_by_user_id:
            return user
        referrer = await self.session.scalar(select(User).where(User.referral_code == normalized))
        if referrer is None or referrer.id == user.id:
            return user
        if await self.has_user_transactions(user.id) or await self.has_paid_subscription_history(user.id):
            return user
        user.referred_by_user_id = referrer.id
        await self.session.flush()
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="referral_bound",
            message="Пользователь зарегистрирован по реферальной ссылке.",
            payload={"user_id": user.id, "referrer_user_id": referrer.id},
        )
        return user

    async def grant_referral_bonus_if_eligible(self, user_id: str, amount_rub: Decimal = Decimal("100")) -> bool:
        user = await self.get_user(user_id)
        if not user.referred_by_user_id or user.referral_reward_granted_at is not None:
            return False
        referrer = await self.get_user(user.referred_by_user_id)
        await self.adjust_balance(
            user_id=referrer.id,
            amount_rub=Decimal(amount_rub),
            transaction_type=BalanceTransactionType.REFERRAL_BONUS,
            description=f"Реферальный бонус за пользователя {user.telegram_id}",
        )
        user.referral_reward_granted_at = utc_now()
        self.session.add(
            Notification(
                user_id=referrer.id,
                type=NotificationType.REFERRAL_BONUS,
                message=f"На ваш баланс начислено {Decimal(amount_rub):.2f} ₽ по реферальной программе.",
            )
        )
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="referral_bonus_granted",
            message="Начислен реферальный бонус.",
            payload={"referrer_user_id": referrer.id, "user_id": user.id, "amount": str(amount_rub)},
        )
        await self.session.flush()
        return True

    async def list_user_targets(self) -> list[User]:
        return list((await self.session.scalars(select(User).order_by(User.created_at.asc()))).all())

    async def list_admin_telegram_ids(self) -> list[int]:
        ids = {int(item) for item in self.settings.admin_allowed_telegram_ids if int(item) > 0}
        rows = await self.session.scalars(
            select(AdminUser.telegram_id).where(AdminUser.is_active.is_(True), AdminUser.telegram_id.is_not(None))
        )
        ids.update(int(item) for item in rows.all() if item and int(item) > 0)
        return sorted(ids)

    async def delete_user_account(
        self,
        user_id: str,
        *,
        actor_admin_id: str | None = None,
    ) -> dict[str, object]:
        user = await self.get_user(user_id)
        remote_uuid = user.remnawave_user_uuid
        if self.remnawave is not None and remote_uuid:
            try:
                await self.remnawave.delete_user(remote_uuid)
            except Exception:
                await self.log_event(
                    level=SystemEventLevel.WARNING,
                    event_type="user_remote_delete_failed",
                    message="Не удалось удалить пользователя из Remnawave, локальное удаление продолжено.",
                    payload={"user_id": user.id, "remote_user_uuid": remote_uuid},
                    actor_admin_id=actor_admin_id,
                )

        deleted_payload = {
            "user_id": user.id,
            "telegram_id": user.telegram_id,
            "username": user.username,
            "remote_user_uuid": remote_uuid,
        }
        await self.session.execute(delete(User).where(User.id == user.id))
        await self.log_event(
            level=SystemEventLevel.WARNING,
            event_type="user_deleted",
            message="Аккаунт пользователя удалён из локальной базы.",
            payload=deleted_payload,
            actor_admin_id=actor_admin_id,
        )
        await self.session.flush()
        return deleted_payload

    async def _notify_topup_approved(self, user_id: str, amount: Decimal) -> None:
        self.session.add(
            Notification(
                user_id=user_id,
                type=NotificationType.TOPUP_APPROVED,
                message=topup_approved_message(amount),
            )
        )

    async def _notify_topup_rejected(self, user_id: str, amount: Decimal, comment: str | None) -> None:
        self.session.add(
            Notification(
                user_id=user_id,
                type=NotificationType.TOPUP_REJECTED,
                message=topup_rejected_message(amount, comment),
            )
        )

    def _apply_remote_user(self, user: User, remote: RemoteUser) -> None:
        user.remnawave_user_uuid = remote.uuid
        user.remnawave_username = remote.username
        user.remnawave_short_uuid = remote.shortUuid

    async def _generate_unique_referral_code(self) -> str:
        for _ in range(20):
            candidate = uuid4().hex[:8].upper()
            existing = await self.session.scalar(select(User.id).where(User.referral_code == candidate))
            if existing is None:
                return candidate
        raise ConflictError("Не удалось создать уникальный реферальный код.")
