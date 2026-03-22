from __future__ import annotations

from decimal import Decimal

from sqlalchemy import String, cast, desc, func, select

from altlink.application.services.base import ServiceBase
from altlink.domain.enums import TopupRequestStatus, UserStatus
from altlink.infrastructure.db.models import OnlineSessionCache, Server, Subscription, TopupRequest, User


class DashboardService(ServiceBase):
    async def get_dashboard(self) -> dict:
        user_status_counts = {
            status.value: count
            for status, count in (
                await self.session.execute(select(User.status, func.count(User.id)).group_by(User.status))
            ).all()
        }
        new_topups = int(
            (
                await self.session.execute(
                    select(func.count(TopupRequest.id)).where(TopupRequest.status == TopupRequestStatus.NEW)
                )
            ).scalar_one()
            or 0
        )
        debts = (
            await self.session.execute(
                select(func.coalesce(func.sum(Subscription.debt_rub), Decimal("0.00"))).where(
                    Subscription.is_current.is_(True)
                )
            )
        ).scalar_one()
        top_users = (
            await self.session.execute(
                select(User, Subscription)
                .join(Subscription, Subscription.user_id == User.id)
                .where(Subscription.is_current.is_(True))
                .order_by(desc(Subscription.traffic_used_bytes_cache))
                .limit(10)
            )
        ).all()
        servers = (await self.session.execute(select(Server).order_by(Server.load_percent.desc()))).scalars().all()
        online = int(
            (
                await self.session.execute(
                    select(func.count(OnlineSessionCache.id)).where(OnlineSessionCache.is_online.is_(True))
                )
            ).scalar_one()
            or 0
        )
        total_traffic = int(
            (
                await self.session.execute(
                    select(func.coalesce(func.sum(Subscription.traffic_used_bytes_cache), 0)).where(
                        Subscription.is_current.is_(True)
                    )
                )
            ).scalar_one()
            or 0
        )
        return {
            "counts": {
                "active": user_status_counts.get(UserStatus.ACTIVE.value, 0),
                "grace": user_status_counts.get(UserStatus.GRACE.value, 0),
                "blocked": user_status_counts.get(UserStatus.BLOCKED.value, 0),
                "trial": user_status_counts.get(UserStatus.TRIAL.value, 0),
                "new": user_status_counts.get(UserStatus.NEW.value, 0),
                "online": online,
                "new_topups": new_topups,
            },
            "debts_rub": debts,
            "total_traffic_bytes": total_traffic,
            "servers": servers,
            "top_users": top_users,
        }

    async def search_users(self, query: str) -> list[User]:
        pattern = f"%{query.lower()}%"
        result = await self.session.execute(
            select(User).where(
                func.lower(func.coalesce(User.telegram_username, "")).like(pattern)
                | cast(User.telegram_id, String).like(pattern)
                | func.lower(func.coalesce(User.first_name, "")).like(pattern)
                | func.lower(func.coalesce(User.last_name, "")).like(pattern)
            )
        )
        return result.scalars().all()
