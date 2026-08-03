from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Text, cast, func, or_, select, text
from sqlalchemy.orm import joinedload

from altlink.application.services.base import BaseService
from altlink.domain.enums import BalanceTransactionType, ServerType, SubscriptionStatus, TopupStatus, UserStatus
from altlink.domain.plans import is_metered_plan_code, is_unlimited_plan_code
from altlink.infrastructure.db.models import (
    BalanceTransaction,
    OnlineSessionCache,
    Plan,
    Server,
    Subscription,
    SystemEvent,
    SystemSetting,
    TrafficSnapshot,
    TopupRequest,
    User,
)
from altlink.utils.time import ensure_utc, to_moscow, utc_now


@dataclass(slots=True)
class UserMetricRow:
    user: User
    value: Decimal | int


@dataclass(slots=True)
class TrafficLeaderboardRow:
    user: User
    plan: Plan | None
    traffic_used_bytes: int
    whitelist_traffic_used_bytes: int
    auto_renew: bool


@dataclass(slots=True)
class DashboardWindow:
    key: str
    label: str
    start: datetime
    end: datetime
    bucket_seconds: int
    labels: list[str]


DASHBOARD_PERIODS: dict[str, tuple[str, timedelta, int]] = {
    "1h": ("1 час", timedelta(hours=1), 12),
    "1d": ("1 день", timedelta(days=1), 24),
    "1w": ("1 неделя", timedelta(days=7), 7),
    "2w": ("2 недели", timedelta(days=14), 14),
    "1m": ("1 месяц", timedelta(days=30), 30),
}
DEFAULT_DASHBOARD_PERIOD = "2w"


class DashboardService(BaseService):
    source = "dashboard"

    async def overview(self, period: str = DEFAULT_DASHBOARD_PERIOD) -> dict:
        window = self._dashboard_window(period)
        subscriptions = list(
            (
                await self.session.scalars(
                    select(Subscription).options(joinedload(Subscription.plan), joinedload(Subscription.user))
                )
            ).all()
        )
        servers = list(
            (
                await self.session.scalars(
                    select(Server)
                    .order_by(Server.load_percent.desc(), Server.users_online.desc())
                )
            ).all()
        )
        topups = list(
            (
                await self.session.scalars(
                    select(TopupRequest)
                    .options(joinedload(TopupRequest.user))
                    .order_by(TopupRequest.created_at.desc())
                )
            ).all()
        )

        user_status_counts = await self._user_status_counts()
        latest_subscriptions = self._latest_subscriptions(subscriptions)
        top_users = await self._traffic_leaderboard_rows(subscriptions)
        plan_mix = self._paid_plan_mix(latest_subscriptions)
        whitelist_traffic = sum(item.whitelist_traffic_used_bytes for item in top_users)
        total_traffic = sum(item.traffic_used_bytes for item in top_users)
        renewal_disabled_users = len(
            [
                item
                for item in latest_subscriptions
                if item.plan and not item.plan.is_trial and not item.auto_renew and item.status == SubscriptionStatus.ACTIVE
            ]
        )

        payments_chart = await self._payments_chart(window)
        users_chart = await self._users_chart(window, subscriptions)
        conversion_funnel = await self._conversion_funnel(window, subscriptions)
        traffic_chart = await self._traffic_chart(window)
        plan_signups_chart = self._plan_signups_chart(window, subscriptions)
        period_topup_total = Decimal(str(sum(payments_chart["values"]))).quantize(Decimal("0.01"))

        return {
            "period": window.key,
            "period_label": window.label,
            "period_options": self.period_options(),
            "active_users": user_status_counts.get(UserStatus.ACTIVE.value, 0),
            "renewal_disabled_users": renewal_disabled_users,
            "blocked_users": user_status_counts.get(UserStatus.BLOCKED.value, 0),
            "trial_users": user_status_counts.get(UserStatus.TRIAL.value, 0),
            "new_users_in_period": sum(users_chart["datasets"]["new_users"]),
            "new_paid_users_in_period": sum(users_chart["datasets"]["new_paid_users"]),
            "payments_count": sum(payments_chart["counts"]),
            "payments_total_rub": period_topup_total,
            "total_traffic_bytes": total_traffic,
            "whitelist_traffic_bytes": whitelist_traffic,
            "servers": servers[:12],
            "top_users": top_users[:10],
            "recent_topups": topups[:12],
            "charts": {
                "user_statuses": {
                    "labels": ["Активные", "Без продления", "Заблокированные", "Тестовые", "Новые"],
                    "values": [
                        user_status_counts.get(UserStatus.ACTIVE.value, 0),
                        renewal_disabled_users,
                        user_status_counts.get(UserStatus.BLOCKED.value, 0),
                        user_status_counts.get(UserStatus.TRIAL.value, 0),
                        user_status_counts.get(UserStatus.NEW.value, 0),
                    ],
                },
                "users": users_chart,
                "paid_users": {
                    "labels": users_chart["labels"],
                    "values": users_chart["datasets"]["new_paid_users"],
                },
                "conversion_funnel": conversion_funnel,
                "plan_signups": plan_signups_chart,
                "payments": payments_chart,
                "traffic": traffic_chart,
                "plan_mix": {
                    "labels": ["Start", "Pro"],
                    "values": [plan_mix["start"], plan_mix["pro"]],
                },
                "server_types": {
                    "labels": ["⚡ 10 Гбит", "Белые списки", "Обычные"],
                    "values": [
                        len([server for server in servers if server.server_type == ServerType.TEN_GBIT]),
                        len([server for server in servers if server.server_type == ServerType.WHITELIST]),
                        len([server for server in servers if server.server_type == ServerType.REGULAR]),
                    ],
                },
            },
        }

    @classmethod
    def period_options(cls) -> list[dict[str, str]]:
        return [{"value": key, "label": value[0]} for key, value in DASHBOARD_PERIODS.items()]

    async def top_users(self, metric: str, limit: int = 10) -> list[UserMetricRow]:
        subscriptions = list(
            (
                await self.session.scalars(
                    select(Subscription).options(joinedload(Subscription.plan), joinedload(Subscription.user))
                )
            ).all()
        )
        users = list((await self.session.scalars(select(User))).all())
        topups = list(
            (
                await self.session.scalars(
                    select(TopupRequest)
                    .options(joinedload(TopupRequest.user))
                    .where(TopupRequest.status == TopupStatus.APPROVED)
                )
            ).all()
        )

        latest_subscriptions = self._latest_subscriptions(subscriptions)
        if metric == "traffic":
            traffic_rows = await self._traffic_leaderboard_rows(subscriptions)
            rows = [UserMetricRow(item.user, item.traffic_used_bytes) for item in traffic_rows if item.user]
        elif metric == "whitelist":
            rows = [
                UserMetricRow(item.user, item.whitelist_traffic_used_bytes)
                for item in latest_subscriptions
                if item.user
            ]
        elif metric == "balance":
            rows = [UserMetricRow(user, Decimal(user.balance_rub)) for user in users]
        elif metric == "topups":
            totals: dict[str, UserMetricRow] = {}
            for item in topups:
                if not item.user:
                    continue
                existing = totals.get(item.user_id)
                if existing is None:
                    totals[item.user_id] = UserMetricRow(item.user, Decimal(item.amount_rub))
                else:
                    existing.value = Decimal(existing.value) + Decimal(item.amount_rub)
            rows = list(totals.values())
        else:
            raise ValueError(f"Unsupported metric: {metric}")

        rows.sort(key=lambda item: item.value, reverse=True)
        return rows[:limit]

    async def list_transactions(
        self,
        *,
        search: str | None = None,
        transaction_type: BalanceTransactionType | None = None,
        amount_min: Decimal | None = None,
        amount_max: Decimal | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 100,
    ) -> list[BalanceTransaction]:
        query = (
            select(BalanceTransaction)
            .join(BalanceTransaction.user)
            .options(joinedload(BalanceTransaction.user), joinedload(BalanceTransaction.created_by_admin))
        )
        normalized_search = (search or "").strip()
        if normalized_search:
            pattern = f"%{normalized_search}%"
            query = query.where(
                or_(
                    User.username.ilike(pattern),
                    cast(User.telegram_id, Text).ilike(pattern),
                    User.id.ilike(pattern),
                    BalanceTransaction.description.ilike(pattern),
                )
            )
        if transaction_type is not None:
            query = query.where(BalanceTransaction.type == transaction_type)
        if amount_min is not None:
            query = query.where(BalanceTransaction.amount_rub >= amount_min)
        if amount_max is not None:
            query = query.where(BalanceTransaction.amount_rub <= amount_max)
        if created_from is not None:
            query = query.where(BalanceTransaction.created_at >= created_from)
        if created_to is not None:
            query = query.where(BalanceTransaction.created_at <= created_to)
        return list(
            (
                await self.session.scalars(
                    query.order_by(BalanceTransaction.created_at.desc()).limit(min(max(int(limit), 1), 500))
                )
            ).all()
        )

    async def list_topups(self, status: TopupStatus | None = None, limit: int = 100) -> list[TopupRequest]:
        query = (
            select(TopupRequest)
            .options(joinedload(TopupRequest.user), joinedload(TopupRequest.approved_by_admin))
            .order_by(TopupRequest.created_at.desc())
            .limit(limit)
        )
        if status is not None:
            query = query.where(TopupRequest.status == status)
        return list((await self.session.scalars(query)).all())

    async def list_plans(self) -> list[Plan]:
        return list((await self.session.scalars(select(Plan).order_by(Plan.sort_order.asc()))).all())

    async def list_traffic_rows(self) -> list[TrafficLeaderboardRow]:
        subscriptions = list(
            (
                await self.session.scalars(
                    select(Subscription).options(joinedload(Subscription.plan), joinedload(Subscription.user))
                )
            ).all()
        )
        return await self._traffic_leaderboard_rows(subscriptions)

    async def list_settings(self) -> list[SystemSetting]:
        return list((await self.session.scalars(select(SystemSetting).order_by(SystemSetting.key.asc()))).all())

    async def list_events(self, limit: int = 100) -> list[SystemEvent]:
        return list((await self.session.scalars(select(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(limit))).all())

    async def panel_status(self) -> dict:
        remnawave_ok = False
        if self.remnawave is not None and getattr(self.remnawave, "base_url", ""):
            remnawave_ok = await self.remnawave.healthcheck()

        db_bytes = 0
        dialect = self.session.bind.dialect.name if self.session.bind is not None else "unknown"
        if dialect == "postgresql":
            db_bytes = int((await self.session.scalar(text("SELECT pg_database_size(current_database())"))) or 0)
        elif dialect == "sqlite":
            db_url = self.settings.database_url.split("///", 1)[-1]
            db_path = Path(db_url)
            if db_path.exists():
                db_bytes = db_path.stat().st_size

        return {
            "remnawave_ok": remnawave_ok,
            "db_size_bytes": db_bytes,
            "db_size_gb": round(db_bytes / 1024**3, 4) if db_bytes else 0,
            "database_dialect": dialect,
        }

    def _dashboard_window(self, period: str) -> DashboardWindow:
        key = period if period in DASHBOARD_PERIODS else DEFAULT_DASHBOARD_PERIOD
        label, duration, bucket_count = DASHBOARD_PERIODS[key]
        end = utc_now()
        start = end - duration
        bucket_seconds = max(int(duration.total_seconds() // bucket_count), 1)
        labels = []
        for index in range(bucket_count):
            bucket_start = start + timedelta(seconds=bucket_seconds * index)
            local_bucket_start = to_moscow(bucket_start)
            labels.append(
                f"{local_bucket_start:%H:%M} МСК"
                if duration <= timedelta(days=1)
                else f"{local_bucket_start:%d.%m} МСК"
            )
        return DashboardWindow(
            key=key,
            label=label,
            start=start,
            end=end,
            bucket_seconds=bucket_seconds,
            labels=labels,
        )

    def _bucket_index(self, window: DashboardWindow, value: datetime | None) -> int | None:
        if value is None:
            return None
        current = ensure_utc(value)
        if current < window.start or current > window.end:
            return None
        index = int((current - window.start).total_seconds() // window.bucket_seconds)
        return min(max(index, 0), len(window.labels) - 1)

    async def _user_status_counts(self) -> dict[str, int]:
        rows = (
            await self.session.execute(
                select(User.status, func.count(User.id)).group_by(User.status)
            )
        ).all()
        return {status.value if hasattr(status, "value") else str(status): int(count) for status, count in rows}

    def _user_snapshot_buckets(self, window: DashboardWindow) -> list[tuple[datetime, datetime, str]]:
        if window.key in {"1h", "1d"}:
            local_end = to_moscow(window.end)
            label = f"{local_end:%H:%M} МСК" if window.key == "1h" else f"{local_end:%d.%m} МСК"
            return [(window.start, window.end, label)]

        buckets = []
        for index, label in enumerate(window.labels):
            start = window.start + timedelta(seconds=window.bucket_seconds * index)
            end = min(start + timedelta(seconds=window.bucket_seconds), window.end)
            buckets.append((start, end, label))
        return buckets

    @staticmethod
    def _subscription_access_ends_at(subscription: Subscription) -> datetime:
        access_ends_at = ensure_utc(subscription.ends_at)
        if subscription.grace_until is not None:
            access_ends_at = max(access_ends_at, ensure_utc(subscription.grace_until))
        candidates = [access_ends_at]
        if subscription.canceled_at is not None:
            candidates.append(ensure_utc(subscription.canceled_at))
        if subscription.blocked_at is not None:
            candidates.append(ensure_utc(subscription.blocked_at))
        return min(candidates)

    def _subscription_was_active_at(self, subscription: Subscription, moment: datetime) -> bool:
        if subscription.status == SubscriptionStatus.PENDING:
            return False
        starts_at = ensure_utc(subscription.created_at or subscription.started_at)
        return starts_at <= moment < self._subscription_access_ends_at(subscription)

    async def _users_chart(self, window: DashboardWindow, subscriptions: list[Subscription]) -> dict:
        buckets = self._user_snapshot_buckets(window)
        labels = [label for _, _, label in buckets]
        active_paid_users = [0 for _ in labels]
        new_users = [0 for _ in labels]
        new_paid_users = [0 for _ in labels]
        trial_users = [0 for _ in labels]

        user_created_rows = (
            await self.session.execute(
                select(User.created_at).where(User.created_at >= window.start, User.created_at <= window.end)
            )
        ).all()
        for (created_at,) in user_created_rows:
            current = ensure_utc(created_at)
            for index, (bucket_start, bucket_end, _) in enumerate(buckets):
                if bucket_start <= current <= bucket_end:
                    new_users[index] += 1
                    break

        first_paid_by_user: dict[str, datetime] = {}
        for subscription in subscriptions:
            if (
                subscription.plan is None
                or subscription.plan.is_trial
                or subscription.status == SubscriptionStatus.PENDING
            ):
                continue
            created_at = ensure_utc(subscription.created_at)
            current = first_paid_by_user.get(subscription.user_id)
            if current is None or created_at < current:
                first_paid_by_user[subscription.user_id] = created_at
        for first_paid_at in first_paid_by_user.values():
            for index, (bucket_start, bucket_end, _) in enumerate(buckets):
                if bucket_start <= first_paid_at <= bucket_end:
                    new_paid_users[index] += 1
                    break

        for index, (_, bucket_end, _) in enumerate(buckets):
            snapshot_at = bucket_end - timedelta(microseconds=1)
            paid_ids: set[str] = set()
            trial_ids: set[str] = set()
            for subscription in subscriptions:
                if subscription.plan is None or not self._subscription_was_active_at(subscription, snapshot_at):
                    continue
                if subscription.plan.is_trial:
                    trial_ids.add(subscription.user_id)
                else:
                    paid_ids.add(subscription.user_id)
            active_paid_users[index] = len(paid_ids)
            trial_users[index] = len(trial_ids - paid_ids)

        return {
            "labels": labels,
            "datasets": {
                "active_paid_users": active_paid_users,
                "new_users": new_users,
                "new_paid_users": new_paid_users,
                "trial_users": trial_users,
            },
            "series": [
                {
                    "key": "active_paid_users",
                    "label": "Активные платные",
                    "values": active_paid_users,
                    "chart_type": "line",
                },
                {
                    "key": "trial_users",
                    "label": "Активные тестовые",
                    "values": trial_users,
                    "chart_type": "line",
                },
                {
                    "key": "new_users",
                    "label": "Новые пользователи",
                    "values": new_users,
                    "chart_type": "bar",
                },
            ],
        }

    async def _conversion_funnel(self, window: DashboardWindow, subscriptions: list[Subscription]) -> dict:
        cohort_rows = (
            await self.session.execute(
                select(User.id).where(User.created_at >= window.start, User.created_at <= window.end)
            )
        ).all()
        cohort_ids = {user_id for (user_id,) in cohort_rows}

        trial_started_by_user: dict[str, datetime] = {}
        paid_started_by_user: dict[str, list[datetime]] = {}
        for subscription in subscriptions:
            if (
                subscription.user_id not in cohort_ids
                or subscription.plan is None
                or subscription.status == SubscriptionStatus.PENDING
            ):
                continue
            created_at = ensure_utc(subscription.created_at)
            if created_at > window.end:
                continue
            if subscription.plan.is_trial:
                current = trial_started_by_user.get(subscription.user_id)
                if current is None or created_at < current:
                    trial_started_by_user[subscription.user_id] = created_at
            else:
                paid_started_by_user.setdefault(subscription.user_id, []).append(created_at)

        trial_user_ids = set(trial_started_by_user)
        paid_after_trial_user_ids = {
            user_id
            for user_id, paid_dates in paid_started_by_user.items()
            if user_id in trial_started_by_user
            and any(paid_at >= trial_started_by_user[user_id] for paid_at in paid_dates)
        }
        connected_user_ids: set[str] = set()
        if trial_user_ids:
            traffic_rows = (
                await self.session.execute(
                    select(TrafficSnapshot.user_id, TrafficSnapshot.created_at)
                    .join(User, TrafficSnapshot.user_id == User.id)
                    .where(
                        User.created_at >= window.start,
                        User.created_at <= window.end,
                        TrafficSnapshot.created_at <= window.end,
                        or_(TrafficSnapshot.used_bytes > 0, TrafficSnapshot.lifetime_used_bytes > 0),
                    )
                )
            ).all()
            for user_id, created_at in traffic_rows:
                trial_started_at = trial_started_by_user.get(user_id)
                if trial_started_at is not None and ensure_utc(created_at) >= trial_started_at:
                    connected_user_ids.add(user_id)

            online_rows = (
                await self.session.execute(
                    select(OnlineSessionCache.user_id, OnlineSessionCache.last_activity_at)
                    .join(User, OnlineSessionCache.user_id == User.id)
                    .where(
                        User.created_at >= window.start,
                        User.created_at <= window.end,
                        OnlineSessionCache.last_activity_at.is_not(None),
                        OnlineSessionCache.last_activity_at <= window.end,
                    )
                )
            ).all()
            for user_id, last_activity_at in online_rows:
                trial_started_at = trial_started_by_user.get(user_id)
                if trial_started_at is not None and ensure_utc(last_activity_at) >= trial_started_at:
                    connected_user_ids.add(user_id)

            for subscription in subscriptions:
                if (
                    subscription.user_id in trial_user_ids
                    and ensure_utc(subscription.created_at) >= trial_started_by_user[subscription.user_id]
                    and (
                        int(subscription.traffic_used_bytes or 0) > 0
                        or int(subscription.whitelist_traffic_used_bytes or 0) > 0
                    )
                ):
                    connected_user_ids.add(subscription.user_id)

        converted_user_ids = connected_user_ids & paid_after_trial_user_ids
        counts = [
            len(cohort_ids),
            len(trial_user_ids),
            len(connected_user_ids),
            len(converted_user_ids),
        ]
        labels = [
            "Новые пользователи",
            "Взяли тест",
            "Подключились",
            "Купили подписку",
        ]

        def percentage(value: int, total: int) -> float:
            return round(value / total * 100, 1) if total > 0 else 0.0

        stages = []
        for index, (key, label, count) in enumerate(
            zip(("new", "trial", "connected", "paid"), labels, counts, strict=True)
        ):
            previous_count = counts[index - 1] if index > 0 else counts[0]
            stages.append(
                {
                    "key": key,
                    "label": label,
                    "count": count,
                    "percent_of_new": percentage(count, counts[0]),
                    "percent_from_previous": percentage(count, previous_count),
                }
            )
        return {
            "labels": labels,
            "counts": counts,
            "percentages": [stage["percent_of_new"] for stage in stages],
            "stages": stages,
        }

    async def _payments_chart(self, window: DashboardWindow) -> dict:
        values = [0.0 for _ in window.labels]
        counts = [0 for _ in window.labels]
        rows = (
            await self.session.execute(
                select(BalanceTransaction.created_at, BalanceTransaction.amount_rub)
                .where(
                    BalanceTransaction.type == BalanceTransactionType.TOPUP,
                    BalanceTransaction.created_at >= window.start,
                    BalanceTransaction.created_at <= window.end,
                )
                .order_by(BalanceTransaction.created_at.asc())
            )
        ).all()
        for created_at, amount in rows:
            index = self._bucket_index(window, created_at)
            if index is None:
                continue
            values[index] += float(Decimal(amount))
            counts[index] += 1
        return {"labels": list(window.labels), "values": values, "counts": counts}

    async def _traffic_chart(self, window: DashboardWindow) -> dict:
        total = [0.0 for _ in window.labels]
        whitelist = [0.0 for _ in window.labels]
        rows = (
            await self.session.execute(
                select(TrafficSnapshot.created_at, TrafficSnapshot.used_bytes, Server.server_type)
                .outerjoin(Server, TrafficSnapshot.server_id == Server.id)
                .where(TrafficSnapshot.created_at >= window.start, TrafficSnapshot.created_at <= window.end)
                .order_by(TrafficSnapshot.created_at.asc())
            )
        ).all()
        for created_at, used_bytes, server_type in rows:
            index = self._bucket_index(window, created_at)
            if index is None:
                continue
            gb = round(int(used_bytes or 0) / 1024**3, 4)
            total[index] += gb
            if server_type == ServerType.WHITELIST:
                whitelist[index] += gb
        return {
            "labels": list(window.labels),
            "datasets": {
                "total_gb": total,
                "whitelist_gb": whitelist,
            },
        }

    def _plan_signups_chart(self, window: DashboardWindow, subscriptions: list[Subscription]) -> dict:
        labels = list(window.labels)
        series: dict[str, list[int]] = {}
        for item in subscriptions:
            if item.plan is None or item.status == SubscriptionStatus.PENDING:
                continue
            index = self._bucket_index(window, item.created_at)
            if index is None:
                continue
            label = item.plan.name or item.plan.code.value
            series.setdefault(label, [0 for _ in labels])[index] += 1
        return {
            "labels": labels,
            "datasets": [
                {"label": label, "values": values}
                for label, values in sorted(series.items(), key=lambda item: item[0].lower())
            ],
        }

    def _latest_subscriptions(self, subscriptions: list[Subscription]) -> list[Subscription]:
        latest: dict[str, Subscription] = {}
        for item in subscriptions:
            current = latest.get(item.user_id)
            if current is None or ensure_utc(item.created_at) > ensure_utc(current.created_at):
                latest[item.user_id] = item
        return list(latest.values())

    def _paid_plan_mix(self, subscriptions: list[Subscription]) -> dict[str, int]:
        counts = {"start": 0, "pro": 0}
        for item in subscriptions:
            if (
                item.plan is None
                or item.plan.is_trial
                or item.status not in {SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE}
            ):
                continue
            if is_metered_plan_code(item.plan.code):
                counts["start"] += 1
            elif is_unlimited_plan_code(item.plan.code):
                counts["pro"] += 1
        return counts

    async def _traffic_leaderboard_rows(self, subscriptions: list[Subscription]) -> list[TrafficLeaderboardRow]:
        latest_subscriptions = {item.user_id: item for item in self._latest_subscriptions(subscriptions)}
        ranked_snapshots = (
            select(
                TrafficSnapshot.id.label("snapshot_id"),
                func.row_number()
                .over(
                    partition_by=TrafficSnapshot.user_id,
                    order_by=(TrafficSnapshot.snapshot_date.desc(), TrafficSnapshot.created_at.desc()),
                )
                .label("row_number"),
            )
            .subquery()
        )
        latest_snapshots = {
            item.user_id: item
            for item in (
                await self.session.scalars(
                    select(TrafficSnapshot)
                    .join(ranked_snapshots, TrafficSnapshot.id == ranked_snapshots.c.snapshot_id)
                    .options(joinedload(TrafficSnapshot.user))
                    .where(ranked_snapshots.c.row_number == 1)
                )
            ).all()
        }

        rows: list[TrafficLeaderboardRow] = []
        for user_id in latest_subscriptions.keys() | latest_snapshots.keys():
            subscription = latest_subscriptions.get(user_id)
            snapshot = latest_snapshots.get(user_id)
            user = (
                subscription.user
                if subscription is not None and subscription.user is not None
                else snapshot.user
                if snapshot is not None
                else None
            )
            if user is None:
                continue
            traffic_used_bytes = (
                int(snapshot.lifetime_used_bytes)
                if snapshot is not None
                else int(subscription.traffic_used_bytes)
                if subscription is not None
                else 0
            )
            rows.append(
                TrafficLeaderboardRow(
                    user=user,
                    plan=subscription.plan if subscription is not None else None,
                    traffic_used_bytes=max(traffic_used_bytes, 0),
                    whitelist_traffic_used_bytes=max(
                        int(subscription.whitelist_traffic_used_bytes) if subscription is not None else 0,
                        0,
                    ),
                    auto_renew=bool(subscription.auto_renew) if subscription is not None else False,
                )
            )
        rows.sort(key=lambda item: item.traffic_used_bytes, reverse=True)
        return rows
