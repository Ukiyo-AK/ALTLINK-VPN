from __future__ import annotations

import logging
from dataclasses import dataclass
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from math import ceil
from uuid import uuid4

import httpx
from sqlalchemy import Text, and_, asc, cast, delete, desc, func, literal, or_, select
from sqlalchemy.orm import aliased, joinedload, selectinload

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
    OnlineSessionCache,
    Notification,
    Plan,
    Subscription,
    TopupRequest,
    TrialPeriod,
    TrafficSnapshot,
    User,
)
from altlink.infrastructure.remnawave_schemas import RemoteHwidDevice, RemoteUser
from altlink.utils.subscriptions import remnawave_public_subscription_url
from altlink.utils.security import hash_password, verify_password
from altlink.utils.time import ensure_utc, utc_now

logger = logging.getLogger(__name__)
USER_LIST_PAGE_SIZE_OPTIONS = (5, 10, 15, 20, 25, 30, 50, 100)
DEFAULT_USER_LIST_PAGE_SIZE = 30


@dataclass(slots=True)
class UserListFilters:
    search: str | None = None
    status: str | None = None
    plan: str | None = None
    balance_min: Decimal | None = None
    balance_max: Decimal | None = None
    last_seen_from: datetime | None = None
    last_seen_to: datetime | None = None
    traffic_min_bytes: int | None = None
    traffic_max_bytes: int | None = None
    whitelist_traffic_min_bytes: int | None = None
    whitelist_traffic_max_bytes: int | None = None
    node_id: str | None = None
    node_traffic_min_bytes: int | None = None
    node_traffic_max_bytes: int | None = None
    next_billing_from: datetime | None = None
    next_billing_to: datetime | None = None
    registered_from: datetime | None = None
    registered_to: datetime | None = None
    device_count_min: int | None = None
    device_count_max: int | None = None
    sort: str = "created_at"
    direction: str = "desc"
    limit: int = DEFAULT_USER_LIST_PAGE_SIZE


@dataclass(slots=True)
class UserListPage:
    users: list[User]
    total: int
    limit: int
    sort: str
    direction: str


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
        filters = UserListFilters(search=search, limit=100)
        return (await self.list_users_for_admin(filters)).users

    async def list_users_for_admin(self, filters: UserListFilters | None = None) -> UserListPage:
        filters = filters or UserListFilters()
        limit = self._normalize_user_list_limit(filters.limit)
        filters.limit = limit

        latest_subscription_rank = (
            select(
                Subscription.id.label("subscription_id"),
                Subscription.user_id.label("user_id"),
                func.row_number()
                .over(partition_by=Subscription.user_id, order_by=Subscription.created_at.desc())
                .label("row_number"),
            )
            .subquery()
        )
        latest_subscription = aliased(Subscription)
        latest_plan = aliased(Plan)

        latest_traffic_rank = (
            select(
                TrafficSnapshot.id.label("snapshot_id"),
                TrafficSnapshot.user_id.label("user_id"),
                func.row_number()
                .over(
                    partition_by=TrafficSnapshot.user_id,
                    order_by=(TrafficSnapshot.snapshot_date.desc(), TrafficSnapshot.created_at.desc()),
                )
                .label("row_number"),
            )
            .subquery()
        )
        latest_traffic = aliased(TrafficSnapshot)

        device_key = func.coalesce(
            func.nullif(OnlineSessionCache.device, ""),
            func.nullif(OnlineSessionCache.remote_ip, ""),
            OnlineSessionCache.id,
        )
        device_counts = (
            select(OnlineSessionCache.user_id.label("user_id"), func.count(func.distinct(device_key)).label("device_count"))
            .where(OnlineSessionCache.user_id.is_not(None))
            .group_by(OnlineSessionCache.user_id)
            .subquery()
        )

        node_traffic = None
        if filters.node_id:
            node_traffic = (
                select(
                    TrafficSnapshot.user_id.label("user_id"),
                    func.max(TrafficSnapshot.lifetime_used_bytes).label("node_traffic_bytes"),
                )
                .where(TrafficSnapshot.server_id == filters.node_id)
                .group_by(TrafficSnapshot.user_id)
                .subquery()
            )

        total_traffic_expr = func.coalesce(latest_traffic.lifetime_used_bytes, latest_subscription.traffic_used_bytes, 0)
        whitelist_traffic_expr = func.coalesce(latest_subscription.whitelist_traffic_used_bytes, 0)
        device_count_expr = func.coalesce(device_counts.c.device_count, 0)
        node_traffic_expr = func.coalesce(node_traffic.c.node_traffic_bytes, 0) if node_traffic is not None else literal(0)

        query = (
            select(
                User,
                latest_subscription,
                latest_plan,
                total_traffic_expr.label("total_traffic_bytes"),
                whitelist_traffic_expr.label("whitelist_traffic_bytes"),
                node_traffic_expr.label("node_traffic_bytes"),
                device_count_expr.label("device_count"),
            )
            .options(joinedload(User.assigned_server))
            .outerjoin(
                latest_subscription_rank,
                and_(
                    latest_subscription_rank.c.user_id == User.id,
                    latest_subscription_rank.c.row_number == 1,
                ),
            )
            .outerjoin(latest_subscription, latest_subscription.id == latest_subscription_rank.c.subscription_id)
            .outerjoin(latest_plan, latest_plan.id == latest_subscription.plan_id)
            .outerjoin(
                latest_traffic_rank,
                and_(
                    latest_traffic_rank.c.user_id == User.id,
                    latest_traffic_rank.c.row_number == 1,
                ),
            )
            .outerjoin(latest_traffic, latest_traffic.id == latest_traffic_rank.c.snapshot_id)
            .outerjoin(device_counts, device_counts.c.user_id == User.id)
        )
        if node_traffic is not None:
            query = query.outerjoin(node_traffic, node_traffic.c.user_id == User.id)

        query = self._apply_user_list_filters(
            query,
            filters,
            latest_subscription=latest_subscription,
            latest_plan=latest_plan,
            total_traffic_expr=total_traffic_expr,
            whitelist_traffic_expr=whitelist_traffic_expr,
            node_traffic_expr=node_traffic_expr,
            device_count_expr=device_count_expr,
        )

        count_query = select(func.count()).select_from(query.with_only_columns(User.id).order_by(None).subquery())
        total = int((await self.session.scalar(count_query)) or 0)

        sort_expr = self._user_list_sort_expression(
            filters.sort,
            latest_subscription=latest_subscription,
            latest_plan=latest_plan,
            total_traffic_expr=total_traffic_expr,
            whitelist_traffic_expr=whitelist_traffic_expr,
            node_traffic_expr=node_traffic_expr,
            device_count_expr=device_count_expr,
        )
        ordered_sort = asc(sort_expr) if filters.direction == "asc" else desc(sort_expr)
        query = query.order_by(ordered_sort, desc(User.created_at)).limit(limit)

        rows = (await self.session.execute(query)).all()
        users: list[User] = []
        for user, subscription, plan, total_traffic, whitelist_traffic, node_traffic_value, device_count in rows:
            setattr(user, "admin_current_subscription", subscription)
            setattr(user, "admin_current_plan", plan)
            setattr(user, "admin_total_traffic_bytes", int(total_traffic or 0))
            setattr(user, "admin_whitelist_traffic_bytes", int(whitelist_traffic or 0))
            setattr(user, "admin_node_traffic_bytes", int(node_traffic_value or 0))
            setattr(user, "admin_device_count", int(device_count or 0))
            users.append(user)

        return UserListPage(
            users=users,
            total=total,
            limit=limit,
            sort=filters.sort,
            direction=filters.direction,
        )

    def _apply_user_list_filters(
        self,
        query,
        filters: UserListFilters,
        *,
        latest_subscription,
        latest_plan,
        total_traffic_expr,
        whitelist_traffic_expr,
        node_traffic_expr,
        device_count_expr,
    ):
        if filters.search:
            normalized = filters.search.strip()
            if normalized:
                username_search = normalized.removeprefix("@")
                search_like = f"%{username_search or normalized}%"
                raw_like = f"%{normalized}%"
                exact_match = normalized.casefold()
                username_exact = username_search.casefold()
                query = query.where(
                    or_(
                        cast(User.telegram_id, Text) == normalized,
                        func.lower(User.id) == exact_match,
                        func.lower(func.coalesce(User.remnawave_user_uuid, "")) == exact_match,
                        func.lower(func.coalesce(User.remnawave_short_uuid, "")) == exact_match,
                        func.lower(func.coalesce(User.username, "")) == username_exact,
                        func.lower(func.coalesce(User.remnawave_username, "")) == username_exact,
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

        status = self._parse_user_status(filters.status)
        if status is not None:
            query = query.where(User.status == status)

        plan_filter = (filters.plan or "").strip()
        if plan_filter == "none":
            query = query.where(latest_subscription.id.is_(None))
        elif plan_filter == "trial":
            query = query.where(latest_plan.is_trial.is_(True))
        elif plan_filter == "paid":
            query = query.where(latest_plan.is_trial.is_(False))
        elif plan_filter == "start":
            query = query.where(latest_plan.code.in_([PlanCode.SINGLE_10GBIT, PlanCode.SINGLE_10GBIT_WEEKLY]))
        elif plan_filter == "pro":
            query = query.where(latest_plan.code.in_([PlanCode.UNLIMITED, PlanCode.UNLIMITED_WEEKLY]))
        elif plan_filter:
            try:
                query = query.where(latest_plan.code == PlanCode(plan_filter))
            except ValueError:
                pass

        if filters.balance_min is not None:
            query = query.where(User.balance_rub >= filters.balance_min)
        if filters.balance_max is not None:
            query = query.where(User.balance_rub <= filters.balance_max)
        if filters.last_seen_from is not None:
            query = query.where(User.last_seen_at >= filters.last_seen_from)
        if filters.last_seen_to is not None:
            query = query.where(User.last_seen_at <= filters.last_seen_to)
        if filters.registered_from is not None:
            query = query.where(User.created_at >= filters.registered_from)
        if filters.registered_to is not None:
            query = query.where(User.created_at <= filters.registered_to)
        if filters.next_billing_from is not None:
            query = query.where(latest_subscription.next_billing_at >= filters.next_billing_from)
        if filters.next_billing_to is not None:
            query = query.where(latest_subscription.next_billing_at <= filters.next_billing_to)
        if filters.traffic_min_bytes is not None:
            query = query.where(total_traffic_expr >= filters.traffic_min_bytes)
        if filters.traffic_max_bytes is not None:
            query = query.where(total_traffic_expr <= filters.traffic_max_bytes)
        if filters.whitelist_traffic_min_bytes is not None:
            query = query.where(whitelist_traffic_expr >= filters.whitelist_traffic_min_bytes)
        if filters.whitelist_traffic_max_bytes is not None:
            query = query.where(whitelist_traffic_expr <= filters.whitelist_traffic_max_bytes)
        if filters.node_id:
            query = query.where(node_traffic_expr > 0)
        if filters.node_traffic_min_bytes is not None:
            query = query.where(node_traffic_expr >= filters.node_traffic_min_bytes)
        if filters.node_traffic_max_bytes is not None:
            query = query.where(node_traffic_expr <= filters.node_traffic_max_bytes)
        if filters.device_count_min is not None:
            query = query.where(device_count_expr >= filters.device_count_min)
        if filters.device_count_max is not None:
            query = query.where(device_count_expr <= filters.device_count_max)
        return query

    def _user_list_sort_expression(
        self,
        sort: str,
        *,
        latest_subscription,
        latest_plan,
        total_traffic_expr,
        whitelist_traffic_expr,
        node_traffic_expr,
        device_count_expr,
    ):
        sort_map = {
            "created_at": User.created_at,
            "registration": User.created_at,
            "username": func.lower(func.coalesce(User.username, "")),
            "status": User.status,
            "balance": User.balance_rub,
            "last_seen": User.last_seen_at,
            "traffic": total_traffic_expr,
            "whitelist_traffic": whitelist_traffic_expr,
            "node_traffic": node_traffic_expr,
            "next_billing": latest_subscription.next_billing_at,
            "devices": device_count_expr,
            "plan": latest_plan.name,
        }
        return sort_map.get(sort, User.created_at)

    @staticmethod
    def _normalize_user_list_limit(limit: int) -> int:
        return limit if limit in USER_LIST_PAGE_SIZE_OPTIONS else DEFAULT_USER_LIST_PAGE_SIZE

    @staticmethod
    def _parse_user_status(value: str | None) -> UserStatus | None:
        if not value:
            return None
        try:
            return UserStatus(value)
        except ValueError:
            return None

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

    async def list_user_hwid_devices(self, user_id: str) -> list[RemoteHwidDevice]:
        user = await self.get_user(user_id)
        if self.remnawave is None:
            return []
        try:
            await self.ensure_remote_user_link(user)
            if not user.remnawave_user_uuid:
                return []
            return await self.remnawave.get_user_hwid_devices(user.remnawave_user_uuid)
        except Exception as exc:
            logger.warning("Failed to load Remnawave HWID devices for user %s.", user.id, exc_info=True)
            raise ConflictError("Не удалось загрузить устройства из панели. Попробуйте ещё раз чуть позже.") from exc

    async def delete_user_hwid_device(self, user_id: str, hwid: str) -> list[RemoteHwidDevice]:
        user = await self.get_user(user_id)
        if self.remnawave is None:
            raise NotFoundError("Устройство не найдено.")
        try:
            await self.ensure_remote_user_link(user)
            if not user.remnawave_user_uuid:
                raise NotFoundError("Устройство не найдено.")
            return await self.remnawave.delete_user_hwid_device(user.remnawave_user_uuid, hwid)
        except NotFoundError:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise NotFoundError("Устройство уже удалено или не найдено.") from exc
            logger.warning("Failed to delete Remnawave HWID device for user %s.", user.id, exc_info=True)
            raise ConflictError("Не удалось удалить устройство. Попробуйте ещё раз чуть позже.") from exc
        except Exception as exc:
            logger.warning("Failed to delete Remnawave HWID device for user %s.", user.id, exc_info=True)
            raise ConflictError("Не удалось удалить устройство. Попробуйте ещё раз чуть позже.") from exc

    async def revoke_user_subscription_link(self, user_id: str) -> RemoteUser:
        user = await self.get_user(user_id)
        if self.remnawave is None:
            raise ConflictError("Панель временно недоступна. Попробуйте ещё раз чуть позже.")
        await self.ensure_remote_user_link(user)
        if not user.remnawave_user_uuid:
            raise NotFoundError("Ссылка подписки пока недоступна.")
        try:
            remote = await self.remnawave.revoke_user_subscription(user.remnawave_user_uuid)
        except Exception as exc:
            logger.warning("Failed to revoke Remnawave subscription link for user %s.", user.id, exc_info=True)
            raise ConflictError("Не удалось перевыпустить ссылку. Попробуйте ещё раз чуть позже.") from exc
        self._apply_remote_user(user, remote)
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="subscription_link_revoked",
            message="Пользователь перевыпустил ссылку подписки.",
            payload={"user_id": user.id, "remote_user_uuid": remote.uuid},
        )
        await self.session.flush()
        return remote

    async def get_rate_limited_user_vless_keys(self, user_id: str) -> list[str]:
        user = await self.session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            raise NotFoundError("Пользователь не найден.")
        cooldown = timedelta(seconds=max(int(self.settings.vless_keys_download_cooldown_seconds), 0))
        now = utc_now()
        if user.vless_keys_downloaded_at is not None:
            retry_after = cooldown - (now - ensure_utc(user.vless_keys_downloaded_at))
            if retry_after.total_seconds() > 0:
                minutes = max(ceil(retry_after.total_seconds() / 60), 1)
                cooldown_minutes = max(ceil(cooldown.total_seconds() / 60), 1)
                raise ConflictError(
                    f"VLESS-ключи можно получить не чаще одного раза в {cooldown_minutes} мин. "
                    f"Подождите ещё {minutes} мин."
                )
        if self.remnawave is None:
            raise ConflictError("Панель временно недоступна. Попробуйте ещё раз чуть позже.")
        await self.ensure_remote_user_link(user)
        if not user.remnawave_user_uuid:
            raise NotFoundError("VLESS-ключи пока недоступны. Сначала активируйте тариф.")
        try:
            keys = await self.remnawave.get_connection_keys(user.remnawave_user_uuid)
        except Exception as exc:
            logger.warning("Failed to load VLESS keys for user %s.", user.id, exc_info=True)
            raise ConflictError("Не удалось подготовить VLESS-ключи. Попробуйте ещё раз чуть позже.") from exc
        vless_keys = [item for item in keys.enabledKeys if item.lower().startswith("vless://")]
        if not vless_keys:
            raise NotFoundError("Для вашего тарифа сейчас нет доступных VLESS-ключей.")
        user.vless_keys_downloaded_at = now
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="vless_keys_downloaded",
            message="Пользователь запросил файл VLESS-ключей.",
            payload={"user_id": user.id, "keys_count": len(vless_keys)},
        )
        await self.session.flush()
        return vless_keys

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
