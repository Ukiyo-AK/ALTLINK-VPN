from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import joinedload

from altlink.application.services.base import BaseService
from altlink.domain.enums import BalanceTransactionType, ServerType, SubscriptionStatus, TopupStatus, UserStatus
from altlink.domain.plans import is_metered_plan_code, is_unlimited_plan_code
from altlink.infrastructure.db.models import (
    BalanceTransaction,
    Plan,
    Server,
    Subscription,
    SystemEvent,
    SystemSetting,
    TrafficSnapshot,
    TopupRequest,
    User,
)


@dataclass(slots=True)
class UserMetricRow:
    user: User
    value: Decimal | int


@dataclass(slots=True)
class TrafficLeaderboardRow:
    user: User
    plan: Plan | None
    traffic_used_bytes: int
    auto_renew: bool


class DashboardService(BaseService):
    source = "dashboard"

    async def overview(self) -> dict:
        users = list((await self.session.scalars(select(User))).all())
        subscriptions = list(
            (
                await self.session.scalars(
                    select(Subscription).options(joinedload(Subscription.plan), joinedload(Subscription.user))
                )
            ).all()
        )
        servers = list((await self.session.scalars(select(Server).order_by(Server.load_percent.desc()))).all())
        transactions = list(
            (
                await self.session.scalars(
                    select(BalanceTransaction)
                    .options(joinedload(BalanceTransaction.user))
                    .order_by(BalanceTransaction.created_at.desc())
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

        latest_subscriptions = self._latest_subscriptions(subscriptions)
        top_users = await self._traffic_leaderboard_rows(subscriptions)
        plan_mix = self._paid_plan_mix(latest_subscriptions)
        whitelist_traffic = sum(item.whitelist_traffic_used_bytes for item in subscriptions)
        total_traffic = sum(item.traffic_used_bytes for item in subscriptions)
        renewal_disabled_users = len(
            [
                item
                for item in latest_subscriptions
                if item.plan and not item.plan.is_trial and not item.auto_renew and item.status == SubscriptionStatus.ACTIVE
            ]
        )

        since = date.today() - timedelta(days=13)
        payments_series = {since + timedelta(days=index): Decimal("0") for index in range(14)}
        for transaction in transactions:
            if transaction.type != BalanceTransactionType.TOPUP or transaction.created_at.date() not in payments_series:
                continue
            payments_series[transaction.created_at.date()] += Decimal(transaction.amount_rub)

        payments_last_30_days = [
            item
            for item in transactions
            if item.type == BalanceTransactionType.TOPUP and item.created_at.date() >= date.today() - timedelta(days=29)
        ]

        return {
            "active_users": len([user for user in users if user.status == UserStatus.ACTIVE]),
            "renewal_disabled_users": renewal_disabled_users,
            "blocked_users": len([user for user in users if user.status == UserStatus.BLOCKED]),
            "trial_users": len([user for user in users if user.status == UserStatus.TRIAL]),
            "payments_count": len(payments_last_30_days),
            "payments_total_rub": sum((Decimal(item.amount_rub) for item in payments_last_30_days), Decimal("0")),
            "total_traffic_bytes": total_traffic,
            "whitelist_traffic_bytes": whitelist_traffic,
            "servers": servers[:12],
            "top_users": top_users[:10],
            "recent_topups": topups[:12],
            "charts": {
                "user_statuses": {
                    "labels": ["Активные", "Без продления", "Заблокированные", "Тестовые", "Новые"],
                    "values": [
                        len([user for user in users if user.status == UserStatus.ACTIVE]),
                        renewal_disabled_users,
                        len([user for user in users if user.status == UserStatus.BLOCKED]),
                        len([user for user in users if user.status == UserStatus.TRIAL]),
                        len([user for user in users if user.status == UserStatus.NEW]),
                    ],
                },
                "server_loads": {
                    "labels": [self._format_server_name(server) for server in servers[:10]],
                    "values": [float(server.load_percent) for server in servers[:10]],
                    "types": [server.server_type.value for server in servers[:10]],
                },
                "payments": {
                    "labels": [day.strftime("%d.%m") for day in payments_series],
                    "values": [float(value) for value in payments_series.values()],
                },
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

    async def list_transactions(self, limit: int = 100) -> list[BalanceTransaction]:
        return list(
            (
                await self.session.scalars(
                    select(BalanceTransaction)
                    .options(joinedload(BalanceTransaction.user), joinedload(BalanceTransaction.created_by_admin))
                    .order_by(BalanceTransaction.created_at.desc())
                    .limit(limit)
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

    def _format_server_name(self, server: Server) -> str:
        if server.server_type == ServerType.TEN_GBIT:
            return f"⚡ {server.name}"
        if server.server_type == ServerType.WHITELIST:
            return f"WL {server.name}"
        return server.name

    def _latest_subscriptions(self, subscriptions: list[Subscription]) -> list[Subscription]:
        latest: dict[str, Subscription] = {}
        for item in subscriptions:
            current = latest.get(item.user_id)
            if current is None or item.created_at > current.created_at:
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
        snapshots = list(
            (
                await self.session.scalars(
                    select(TrafficSnapshot)
                    .options(joinedload(TrafficSnapshot.user))
                    .order_by(TrafficSnapshot.snapshot_date.desc(), TrafficSnapshot.created_at.desc())
                )
            ).all()
        )
        latest_snapshots: dict[str, TrafficSnapshot] = {}
        for item in snapshots:
            if item.user_id not in latest_snapshots:
                latest_snapshots[item.user_id] = item

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
                    auto_renew=bool(subscription.auto_renew) if subscription is not None else False,
                )
            )
        rows.sort(key=lambda item: item.traffic_used_bytes, reverse=True)
        return rows
