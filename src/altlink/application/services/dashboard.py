from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from altlink.application.services.base import BaseService
from altlink.domain.enums import BalanceTransactionType, ServerType, TopupStatus, UserStatus
from altlink.infrastructure.db.models import (
    BalanceTransaction,
    Plan,
    Server,
    Subscription,
    SystemEvent,
    SystemSetting,
    TopupRequest,
    User,
)


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

        top_users = sorted(subscriptions, key=lambda item: item.traffic_used_bytes, reverse=True)[:10]
        debt_total = sum((Decimal(item.accrued_debt_rub) for item in subscriptions), Decimal("0"))
        whitelist_traffic = sum(item.whitelist_traffic_used_bytes for item in subscriptions)
        total_traffic = sum(item.traffic_used_bytes for item in subscriptions)

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
            "grace_users": len([user for user in users if user.status == UserStatus.GRACE]),
            "blocked_users": len([user for user in users if user.status == UserStatus.BLOCKED]),
            "trial_users": len([user for user in users if user.status == UserStatus.TRIAL]),
            "payments_count": len(payments_last_30_days),
            "payments_total_rub": sum((Decimal(item.amount_rub) for item in payments_last_30_days), Decimal("0")),
            "total_traffic_bytes": total_traffic,
            "whitelist_traffic_bytes": whitelist_traffic,
            "debt_total_rub": debt_total,
            "servers": servers[:12],
            "top_users": top_users,
            "recent_topups": topups[:12],
            "charts": {
                "user_statuses": {
                    "labels": ["Активные", "Grace", "Заблокированные", "Тестовые", "Новые"],
                    "values": [
                        len([user for user in users if user.status == UserStatus.ACTIVE]),
                        len([user for user in users if user.status == UserStatus.GRACE]),
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

    def _format_server_name(self, server: Server) -> str:
        if server.server_type == ServerType.TEN_GBIT:
            return f"⚡ {server.name}"
        if server.server_type == ServerType.WHITELIST:
            return f"WL {server.name}"
        return server.name
