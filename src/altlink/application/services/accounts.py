from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Text, cast, func, or_, select
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
from altlink.utils.security import hash_password, verify_password
from altlink.utils.time import utc_now


class AccountService(BaseService):
    source = "accounts"

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
                joinedload(User.trial_period),
            ],
        )
        if user is None:
            raise NotFoundError("Пользователь не найден.")
        return user

    async def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        return await self.session.scalar(select(User).where(User.telegram_id == telegram_id))

    async def list_users(self, search: str | None = None) -> Sequence[User]:
        query = select(User).order_by(User.created_at.desc())
        if search:
            search_like = f"%{search}%"
            query = query.where(
                or_(
                    User.username.ilike(search_like),
                    User.first_name.ilike(search_like),
                    User.last_name.ilike(search_like),
                    cast(User.telegram_id, Text).ilike(search_like),
                )
            )
        return list((await self.session.scalars(query.limit(200))).all())

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
        if self.remnawave is None or not user.remnawave_user_uuid or not user.remnawave_short_uuid:
            return {"user": user, "subscription": await self.get_current_subscription(user.id)}

        accessible_nodes = await self.remnawave.get_accessible_nodes(user.remnawave_user_uuid)
        connection_keys = await self.remnawave.get_connection_keys(user.remnawave_user_uuid)
        subscription_info = await self.remnawave.get_subscription_info(user.remnawave_short_uuid)

        return {
            "user": user,
            "subscription": await self.get_current_subscription(user.id),
            "accessible_nodes": accessible_nodes,
            "connection_keys": connection_keys,
            "subscription_info": subscription_info,
        }

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
