from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import joinedload

from altlink.application.services.base import BaseService
from altlink.infrastructure.db.models import OnlineSessionCache, Server, User
from altlink.utils.time import utc_now


class OnlineService(BaseService):
    source = "online"

    async def refresh_online_cache(self, detailed: bool = False) -> list[OnlineSessionCache]:
        if self.remnawave is None:
            return []

        now = utc_now()
        users = list((await self.session.scalars(select(User))).all())
        user_map = {user.remnawave_user_uuid: user for user in users if user.remnawave_user_uuid}
        servers = list((await self.session.scalars(select(Server))).all())
        server_map = {server.remnawave_node_uuid: server for server in servers}

        await self.session.execute(delete(OnlineSessionCache))
        created: list[OnlineSessionCache] = []

        for remote_user in await self.remnawave.list_users():
            user = user_map.get(remote_user.uuid)
            if user is None:
                continue

            server = server_map.get(remote_user.userTraffic.lastConnectedNodeUuid or "")
            ip_address = None
            last_agent = remote_user.subLastUserAgent
            if detailed:
                history = await self.remnawave.get_subscription_request_history(remote_user.uuid)
                if history:
                    latest = history[0]
                    ip_address = latest.requestIp
                    last_agent = latest.userAgent or last_agent

            session = OnlineSessionCache(
                user_id=user.id,
                server_id=server.id if server else None,
                remote_ip=ip_address,
                user_agent=last_agent,
                device=last_agent,
                inbound=None,
                last_activity_at=remote_user.userTraffic.onlineAt,
                is_online=bool(
                    remote_user.userTraffic.onlineAt
                    and remote_user.userTraffic.onlineAt >= now - timedelta(minutes=2)
                ),
                raw_payload=remote_user.model_dump(mode="json"),
            )
            self.session.add(session)
            created.append(session)

        return created

    async def list_online(self, only_online: bool = True) -> list[OnlineSessionCache]:
        query = (
            select(OnlineSessionCache)
            .options(joinedload(OnlineSessionCache.user), joinedload(OnlineSessionCache.server))
            .order_by(OnlineSessionCache.last_activity_at.desc().nullslast())
        )
        if only_online:
            query = query.where(OnlineSessionCache.is_online.is_(True))
        return list((await self.session.scalars(query.limit(200))).all())
