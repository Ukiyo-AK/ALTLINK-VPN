from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from altlink.application.services.base import ServiceBase
from altlink.domain.enums import EventLevel, SubscriptionStatus, UserServerAccessStatus
from altlink.infrastructure.db.models import Server, ServerInbound, Subscription, User, UserServerAccess


class ServerService(ServiceBase):
    async def sync_from_remnawave(self) -> list[Server]:
        if self.remnawave is None:
            raise RuntimeError("Remnawave client is required")

        nodes = await self.remnawave.list_nodes()
        inbound_list = await self.remnawave.list_all_inbounds()
        inbound_by_uuid = {item.uuid: item for item in inbound_list}

        current_servers = {server.remnawave_node_uuid: server for server in (await self.session.execute(select(Server))).scalars()}
        synced: list[Server] = []
        now = datetime.now(UTC)
        for node in nodes:
            server = current_servers.get(node.uuid)
            if server is None:
                server = Server(
                    remnawave_node_uuid=node.uuid,
                    name=node.name,
                    address=node.address,
                    port=node.port,
                    country_code=node.countryCode,
                    tags=node.tags,
                    active_config_profile_uuid=node.configProfile.activeConfigProfileUuid,
                    is_online=node.isConnected,
                    is_connected=node.isConnected,
                    is_disabled_remote=node.isDisabled,
                    raw_data=node.model_dump(mode="json"),
                    last_synced_at=now,
                    max_clients_count=1,
                )
                self.session.add(server)
                await self.session.flush()
            else:
                server.name = node.name
                server.address = node.address
                server.port = node.port
                server.country_code = node.countryCode
                server.tags = node.tags
                server.active_config_profile_uuid = node.configProfile.activeConfigProfileUuid
                server.is_online = node.isConnected
                server.is_connected = node.isConnected
                server.is_disabled_remote = node.isDisabled
                server.users_online = node.usersOnline
                server.last_status_message = node.lastStatusMessage
                server.raw_data = node.model_dump(mode="json")
                server.last_synced_at = now

            existing_inbounds = {inbound.remnawave_inbound_uuid: inbound for inbound in server.inbounds}
            for inbound in node.configProfile.activeInbounds:
                inbound_meta = inbound_by_uuid.get(inbound.uuid)
                record = existing_inbounds.get(inbound.uuid)
                if record is None:
                    record = ServerInbound(
                        server_id=server.id,
                        remnawave_inbound_uuid=inbound.uuid,
                    )
                    self.session.add(record)
                record.config_profile_uuid = inbound.profileUuid
                record.config_profile_inbound_uuid = inbound.uuid
                record.tag = inbound.tag
                record.type = inbound.type
                record.network = inbound.network
                record.security = inbound.security
                record.port = inbound.port
                record.raw_inbound = inbound.rawInbound
                record.active_squads = inbound_meta.activeSquads if inbound_meta is not None else []
                record.last_synced_at = now
            synced.append(server)

        await self.recalculate_loads()
        await self.sync_user_server_access()
        await self.log_event(
            scope="servers",
            level=EventLevel.INFO,
            title="Синхронизация серверов с Remnawave завершена",
            details=f"nodes={len(synced)}",
        )
        return synced

    async def recalculate_loads(self) -> None:
        active_user_count_result = await self.session.execute(
            select(func.count(Subscription.id)).where(
                Subscription.is_current.is_(True),
                Subscription.status.in_(
                    [
                        SubscriptionStatus.ACTIVE,
                        SubscriptionStatus.TRIAL,
                        SubscriptionStatus.GRACE,
                    ]
                ),
            )
        )
        active_users = int(active_user_count_result.scalar_one() or 0)
        result = await self.session.execute(select(Server))
        for server in result.scalars():
            current_clients = active_users if server.is_managed and server.is_enabled else 0
            server.current_clients_count = current_clients
            if server.max_clients_count < 1:
                server.max_clients_count = max(current_clients, 1)
            if server.max_clients_count < current_clients:
                server.max_clients_count = current_clients
            server.load_percent = Decimal(current_clients) / Decimal(max(server.max_clients_count, 1)) * Decimal("100.00")
            for inbound in server.inbounds:
                inbound.current_clients_count = current_clients

    async def set_server_management(self, server_id: str, *, is_enabled: bool, max_clients_count: int) -> Server:
        server = await self.session.get(Server, server_id)
        if server is None:
            raise ValueError("Сервер не найден")
        server.is_managed = True
        server.is_enabled = is_enabled
        server.max_clients_count = max_clients_count
        await self.recalculate_loads()
        await self.sync_user_server_access()
        await self.log_event(
            scope="servers",
            level=EventLevel.INFO,
            title="Локальный статус сервера обновлен",
            details=f"{server.name}: enabled={is_enabled}",
            server_id=server.id,
        )
        return server

    async def remove_server_from_local_system(self, server_id: str) -> Server:
        server = await self.session.get(Server, server_id)
        if server is None:
            raise ValueError("Сервер не найден")
        server.is_enabled = False
        server.is_managed = False
        await self.sync_user_server_access()
        await self.log_event(
            scope="servers",
            level=EventLevel.INFO,
            title="Сервер удален из локальной системы",
            server_id=server.id,
        )
        return server

    async def sync_user_server_access(self) -> None:
        users = (
            await self.session.execute(
                select(User)
                .join(Subscription, Subscription.user_id == User.id)
                .where(
                    Subscription.is_current.is_(True),
                    Subscription.status.in_(
                        [
                            SubscriptionStatus.ACTIVE,
                            SubscriptionStatus.TRIAL,
                            SubscriptionStatus.GRACE,
                        ]
                    ),
                )
            )
        ).scalars().all()
        servers = (
            await self.session.execute(select(Server).where(Server.is_managed.is_(True), Server.is_enabled.is_(True)))
        ).scalars().all()
        existing = {(access.user_id, access.server_id): access for access in (await self.session.execute(select(UserServerAccess))).scalars()}
        active_keys = set()
        now = datetime.now(UTC)
        for user in users:
            for server in servers:
                key = (user.id, server.id)
                active_keys.add(key)
                access = existing.get(key)
                if access is None:
                    access = UserServerAccess(
                        user_id=user.id,
                        server_id=server.id,
                        status=UserServerAccessStatus.ACTIVE,
                        last_synced_at=now,
                    )
                    self.session.add(access)
                else:
                    access.status = UserServerAccessStatus.ACTIVE
                    access.last_synced_at = now
        for key, access in existing.items():
            if key not in active_keys:
                access.status = UserServerAccessStatus.REMOVED
                access.last_synced_at = now

    async def list_managed_servers(self) -> list[Server]:
        result = await self.session.execute(select(Server).order_by(Server.name.asc()))
        return result.scalars().all()
