from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from altlink.application.services.base import BaseService
from altlink.domain.enums import TopupStatus, UserStatus
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
        new_topups = await self.session.scalar(
            select(func.count()).select_from(TopupRequest).where(TopupRequest.status == TopupStatus.NEW)
        )

        top_users = sorted(subscriptions, key=lambda item: item.traffic_used_bytes, reverse=True)[:10]
        debt_total = Decimal("0")
        for subscription in subscriptions:
            if subscription.user and Decimal(subscription.user.balance_rub) < Decimal(subscription.plan.price_rub):
                debt_total += Decimal(subscription.plan.price_rub) - Decimal(subscription.user.balance_rub)

        return {
            "active_users": len([user for user in users if user.status == UserStatus.ACTIVE]),
            "grace_users": len([user for user in users if user.status == UserStatus.GRACE]),
            "blocked_users": len([user for user in users if user.status == UserStatus.BLOCKED]),
            "trial_users": len([user for user in users if user.status == UserStatus.TRIAL]),
            "new_topups": int(new_topups or 0),
            "total_traffic_bytes": sum(item.traffic_used_bytes for item in subscriptions),
            "debt_total_rub": debt_total,
            "servers": servers[:10],
            "top_users": top_users,
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
        return list(
            (
                await self.session.scalars(
                    select(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(limit)
                )
            ).all()
        )
