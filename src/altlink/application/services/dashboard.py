from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Text, case, cast, delete, func, or_, select, text
from sqlalchemy.orm import joinedload, selectinload

from altlink.application.services.base import BaseService
from altlink.domain.enums import (
    BalanceTransactionType,
    ServerType,
    SubscriptionStatus,
    SystemEventLevel,
    TopupStatus,
    UserStatus,
)
from altlink.domain.plans import is_metered_plan_code, is_unlimited_plan_code
from altlink.infrastructure.db.models import (
    BalanceTransaction,
    OnlineSessionCache,
    Plan,
    Server,
    ServerMetricSnapshot,
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
SERVER_METRIC_SAMPLE_INTERVAL = timedelta(minutes=5)
SERVER_METRIC_RETENTION = timedelta(days=90)
MAX_ANALYTICS_SERVERS = 8


class DashboardService(BaseService):
    source = "dashboard"

    async def summary(self) -> dict:
        now = utc_now()
        status_counts = await self._user_status_counts()
        servers = list(
            (
                await self.session.scalars(
                    select(Server)
                    .options(selectinload(Server.inbounds))
                    .order_by(Server.is_connected.asc(), Server.name.asc())
                )
            ).all()
        )
        operational_servers = [server for server in servers if self._server_is_operational(server)]
        unavailable_start_ids = {
            server.id
            for server in servers
            if server.server_type == ServerType.TEN_GBIT and not self._server_is_operational(server)
        }
        affected_users: list[dict] = []
        if unavailable_start_ids:
            candidates = list(
                (
                    await self.session.scalars(
                        select(User)
                        .where(
                            User.assigned_server_id.in_(unavailable_start_ids),
                            User.status.in_([UserStatus.ACTIVE, UserStatus.GRACE]),
                        )
                        .options(
                            joinedload(User.assigned_server),
                            selectinload(User.subscriptions).joinedload(Subscription.plan),
                        )
                        .order_by(User.last_seen_at.desc().nullslast())
                    )
                ).all()
            )
            for user in candidates:
                subscription = self._resolve_current_subscription(user.subscriptions)
                if subscription and subscription.plan and is_metered_plan_code(subscription.plan.code):
                    affected_users.append({"user": user, "server": user.assigned_server})

        payments_count, payments_total = (
            await self.session.execute(
                select(func.count(TopupRequest.id), func.coalesce(func.sum(TopupRequest.amount_rub), 0)).where(
                    TopupRequest.status == TopupStatus.APPROVED,
                    TopupRequest.created_at >= now - timedelta(days=30),
                )
            )
        ).one()
        recent_topups = list(
            (
                await self.session.scalars(
                    select(TopupRequest)
                    .options(joinedload(TopupRequest.user))
                    .order_by(TopupRequest.created_at.desc())
                    .limit(8)
                )
            ).all()
        )
        recent_alerts = list(
            (
                await self.session.scalars(
                    select(SystemEvent)
                    .where(SystemEvent.level.in_([SystemEventLevel.WARNING, SystemEventLevel.ERROR]))
                    .order_by(SystemEvent.created_at.desc())
                    .limit(8)
                )
            ).all()
        )
        new_users_24h = int(
            (
                await self.session.scalar(
                    select(func.count(User.id)).where(User.created_at >= now - timedelta(hours=24))
                )
            )
            or 0
        )
        return {
            "active_users": status_counts.get(UserStatus.ACTIVE.value, 0),
            "trial_users": status_counts.get(UserStatus.TRIAL.value, 0),
            "blocked_users": status_counts.get(UserStatus.BLOCKED.value, 0),
            "new_users_24h": new_users_24h,
            "payments_count_30d": int(payments_count or 0),
            "payments_total_30d": Decimal(str(payments_total or 0)).quantize(Decimal("0.01")),
            "servers_total": len(servers),
            "servers_operational": len(operational_servers),
            "servers_unavailable": len(servers) - len(operational_servers),
            "unavailable_servers": [
                server for server in servers if not self._server_is_operational(server)
            ][:12],
            "affected_start_users": affected_users[:12],
            "affected_start_users_count": len(affected_users),
            "recent_topups": recent_topups,
            "recent_alerts": recent_alerts,
        }

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
                    .limit(12)
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
            "recent_topups": topups,
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

    async def capture_server_metrics(self, *, force: bool = False) -> int:
        now = utc_now()
        latest_capture = await self.session.scalar(select(func.max(ServerMetricSnapshot.captured_at)))
        if (
            not force
            and latest_capture is not None
            and now - ensure_utc(latest_capture) < SERVER_METRIC_SAMPLE_INTERVAL
        ):
            return 0

        servers = list(
            (
                await self.session.scalars(
                    select(Server).options(selectinload(Server.inbounds)).order_by(Server.name.asc())
                )
            ).all()
        )
        for server in servers:
            raw_uptime = (server.raw_payload or {}).get("xrayUptime")
            self.session.add(
                ServerMetricSnapshot(
                    server_id=server.id,
                    remnawave_node_uuid=server.remnawave_node_uuid,
                    server_name=server.name,
                    country_code=(server.country_code or "").upper() or None,
                    server_type=server.server_type.value,
                    is_operational=self._server_is_operational(server),
                    is_connected=bool(server.is_connected),
                    is_available=bool(server.is_available),
                    assigned_users=max(int(server.current_clients or 0), 0),
                    online_users=max(int(server.users_online or 0), 0),
                    xray_uptime=str(raw_uptime)[:64] if raw_uptime is not None else None,
                    captured_at=now,
                )
            )
        await self.session.execute(
            delete(ServerMetricSnapshot).where(
                ServerMetricSnapshot.captured_at < now - SERVER_METRIC_RETENTION
            )
        )
        await self.session.flush()
        return len(servers)

    async def server_analytics(
        self,
        period: str = DEFAULT_DASHBOARD_PERIOD,
        selected_server_ids: Sequence[str] | None = None,
    ) -> dict:
        window = self._dashboard_window(period)
        servers = list(
            (
                await self.session.scalars(
                    select(Server).options(selectinload(Server.inbounds)).order_by(Server.name.asc())
                )
            ).all()
        )
        server_by_id = {server.id: server for server in servers}
        requested_ids = list(dict.fromkeys(selected_server_ids or []))
        selected_ids = [server_id for server_id in requested_ids if server_id in server_by_id][
            :MAX_ANALYTICS_SERVERS
        ]
        if not selected_ids:
            selected_ids = [server.id for server in servers[:4]]

        uptime_rows = (
            await self.session.execute(
                select(
                    ServerMetricSnapshot.server_id,
                    func.count(ServerMetricSnapshot.id),
                    func.sum(case((ServerMetricSnapshot.is_operational.is_(True), 1), else_=0)),
                )
                .where(
                    ServerMetricSnapshot.captured_at >= window.start,
                    ServerMetricSnapshot.captured_at <= window.end,
                )
                .group_by(ServerMetricSnapshot.server_id)
            )
        ).all()
        uptime_by_server = {
            server_id: {
                "samples": int(samples or 0),
                "operational": int(operational or 0),
            }
            for server_id, samples, operational in uptime_rows
        }

        snapshots: list[ServerMetricSnapshot] = []
        if selected_ids:
            snapshots = list(
                (
                    await self.session.scalars(
                        select(ServerMetricSnapshot)
                        .where(
                            ServerMetricSnapshot.server_id.in_(selected_ids),
                            ServerMetricSnapshot.captured_at >= window.start,
                            ServerMetricSnapshot.captured_at <= window.end,
                        )
                        .order_by(ServerMetricSnapshot.captured_at.asc())
                    )
                ).all()
            )

        series_values = {
            server_id: {
                "assigned": [[] for _ in window.labels],
                "online": [[] for _ in window.labels],
                "uptime": [[] for _ in window.labels],
            }
            for server_id in selected_ids
        }
        for snapshot in snapshots:
            bucket_index = self._bucket_index(window, snapshot.captured_at)
            if bucket_index is None or snapshot.server_id not in series_values:
                continue
            bucket = series_values[snapshot.server_id]
            bucket["assigned"][bucket_index].append(int(snapshot.assigned_users or 0))
            bucket["online"][bucket_index].append(int(snapshot.online_users or 0))
            bucket["uptime"][bucket_index].append(100 if snapshot.is_operational else 0)

        def averaged(values: list[list[int]], *, digits: int = 1) -> list[float | None]:
            return [round(sum(bucket) / len(bucket), digits) if bucket else None for bucket in values]

        def server_label(server: Server) -> str:
            country = f" [{server.country_code.upper()}]" if server.country_code else ""
            return f"{server.name}{country}"

        assigned_datasets = []
        online_datasets = []
        uptime_datasets = []
        for server_id in selected_ids:
            server = server_by_id[server_id]
            values = series_values[server_id]
            assigned_datasets.append(
                {"server_id": server_id, "label": server_label(server), "values": averaged(values["assigned"])}
            )
            online_datasets.append(
                {"server_id": server_id, "label": server_label(server), "values": averaged(values["online"])}
            )
            uptime_datasets.append(
                {"server_id": server_id, "label": server_label(server), "values": averaged(values["uptime"])}
            )

        uptime_cards = []
        for server in servers:
            totals = uptime_by_server.get(server.id, {"samples": 0, "operational": 0})
            samples = totals["samples"]
            uptime_cards.append(
                {
                    "server": server,
                    "operational": self._server_is_operational(server),
                    "uptime_percent": round(totals["operational"] * 100 / samples, 2) if samples else None,
                    "sample_count": samples,
                    "xray_uptime": self._format_xray_uptime((server.raw_payload or {}).get("xrayUptime")),
                }
            )

        last_captured_at = await self.session.scalar(select(func.max(ServerMetricSnapshot.captured_at)))
        return {
            "period": window.key,
            "period_label": window.label,
            "period_options": self.period_options(),
            "server_options": servers,
            "selected_server_ids": selected_ids,
            "selection_limit": MAX_ANALYTICS_SERVERS,
            "uptime_cards": uptime_cards,
            "last_captured_at": last_captured_at,
            "charts": {
                "labels": window.labels,
                "assigned_users": assigned_datasets,
                "online_users": online_datasets,
                "uptime": uptime_datasets,
            },
        }

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

    @staticmethod
    def _resolve_current_subscription(subscriptions: Sequence[Subscription]) -> Subscription | None:
        active_states = {SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE}
        candidates = [item for item in subscriptions if item.status in active_states and item.plan is not None]
        candidates.sort(key=lambda item: item.created_at, reverse=True)
        return candidates[0] if candidates else None

    @staticmethod
    def _server_is_operational(server: Server) -> bool:
        has_active_inbounds = any(
            inbound.is_active and inbound.remnawave_inbound_uuid for inbound in server.inbounds
        )
        return bool(server.is_available and server.is_connected and has_active_inbounds)

    @staticmethod
    def _format_xray_uptime(value: object) -> str | None:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        try:
            seconds = max(int(raw), 0)
        except ValueError:
            return raw
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        if days:
            return f"{days} д {hours} ч"
        if hours:
            return f"{hours} ч {minutes} мин"
        return f"{minutes} мин"

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
        baseline_ranked = (
            select(
                TrafficSnapshot.id.label("snapshot_id"),
                func.row_number()
                .over(
                    partition_by=TrafficSnapshot.user_id,
                    order_by=TrafficSnapshot.created_at.desc(),
                )
                .label("row_number"),
            )
            .where(
                TrafficSnapshot.server_id.is_(None),
                TrafficSnapshot.created_at < window.start,
            )
            .subquery()
        )
        baseline_rows = list(
            (
                await self.session.scalars(
                    select(TrafficSnapshot)
                    .join(baseline_ranked, TrafficSnapshot.id == baseline_ranked.c.snapshot_id)
                    .where(baseline_ranked.c.row_number == 1)
                )
            ).all()
        )
        rows = list(
            (
                await self.session.scalars(
                    select(TrafficSnapshot)
                    .where(
                        TrafficSnapshot.server_id.is_(None),
                        TrafficSnapshot.created_at >= window.start,
                        TrafficSnapshot.created_at <= window.end,
                    )
                    .order_by(TrafficSnapshot.user_id.asc(), TrafficSnapshot.created_at.asc())
                )
            ).all()
        )
        new_user_ids = set(
            (
                await self.session.scalars(
                    select(User.id).where(User.created_at >= window.start, User.created_at <= window.end)
                )
            ).all()
        )
        previous_by_user = {
            snapshot.user_id: max(int(snapshot.lifetime_used_bytes or 0), 0)
            for snapshot in baseline_rows
        }
        for snapshot in rows:
            current = max(int(snapshot.lifetime_used_bytes or 0), 0)
            previous = previous_by_user.get(snapshot.user_id)
            if previous is None:
                previous_by_user[snapshot.user_id] = current
                if snapshot.user_id not in new_user_ids:
                    continue
                previous = 0
            delta = current - previous if current >= previous else current
            previous_by_user[snapshot.user_id] = current
            if delta <= 0:
                continue
            index = self._bucket_index(window, snapshot.created_at)
            if index is None:
                continue
            total[index] += round(delta / 1024**3, 4)
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
