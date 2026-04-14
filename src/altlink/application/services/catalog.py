from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from altlink.application.services.base import BaseService, NotFoundError
from altlink.domain.enums import AccessStatus, SystemEventLevel, UserStatus
from altlink.infrastructure.db.models import Server, ServerInbound, User, UserServerAccess
from altlink.utils.time import utc_now


class CatalogService(BaseService):
    source = "catalog"

    async def list_servers(self) -> list[Server]:
        return list(
            (
                await self.session.scalars(
                    select(Server).options(selectinload(Server.inbounds)).order_by(Server.name.asc())
                )
            ).all()
        )

    async def get_server(self, server_id: str) -> Server:
        server = await self.session.get(Server, server_id, options=[selectinload(Server.inbounds)])
        if server is None:
            raise NotFoundError("Сервер не найден.")
        return server

    async def sync_servers(self) -> list[Server]:
        if self.remnawave is None:
            raise RuntimeError("Remnawave клиент не инициализирован.")

        now = utc_now()
        remote_nodes = await self.remnawave.list_nodes()
        current_servers = {
            item.remnawave_node_uuid: item
            for item in (
                await self.session.scalars(select(Server).options(selectinload(Server.inbounds)))
            ).all()
        }

        seen_node_ids: set[str] = set()
        for node in remote_nodes:
            seen_node_ids.add(node.uuid)
            server = current_servers.get(node.uuid)
            if server is None:
                server = Server(
                    remnawave_node_uuid=node.uuid,
                    name=node.name,
                    address=node.address,
                    is_available=True,
                )
                self.session.add(server)
                await self.session.flush()

            server.name = node.name
            server.address = node.address
            server.country_code = node.countryCode
            server.is_connected = node.isConnected
            server.last_status_message = node.lastStatusMessage
            server.last_status_change = node.lastStatusChange
            server.users_online = node.usersOnline or 0
            server.raw_payload = node.model_dump(mode="json")
            server.last_sync_at = now

            inbound_map = {inbound.tag: inbound for inbound in server.inbounds}
            active_tags: set[str] = set()
            for remote_inbound in node.configProfile.activeInbounds:
                active_tags.add(remote_inbound.tag)
                inbound = inbound_map.get(remote_inbound.tag)
                if inbound is None:
                    inbound = ServerInbound(server_id=server.id, tag=remote_inbound.tag, type=remote_inbound.type)
                    self.session.add(inbound)
                inbound.remnawave_inbound_uuid = remote_inbound.uuid
                inbound.type = remote_inbound.type
                inbound.network = remote_inbound.network
                inbound.security = remote_inbound.security
                inbound.port = remote_inbound.port
                inbound.is_active = True
                inbound.raw_payload = remote_inbound.model_dump(mode="json")

            for inbound in server.inbounds:
                if inbound.tag not in active_tags:
                    inbound.is_active = False

        for key, server in current_servers.items():
            if key not in seen_node_ids:
                server.is_connected = False
                server.last_sync_at = now

        await self.rebuild_user_access_matrix()
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="servers_synced",
            message="Список серверов синхронизирован с Remnawave.",
            payload={"count": len(remote_nodes)},
        )
        return await self.list_servers()

    async def set_server_availability(self, server_id: str, is_available: bool) -> Server:
        server = await self.session.get(Server, server_id)
        if server is None:
            raise NotFoundError("Сервер не найден.")
        server.is_available = is_available
        await self.rebuild_user_access_matrix()
        return server

    async def set_server_capacity(self, server_id: str, max_clients: int) -> Server:
        server = await self.session.get(Server, server_id)
        if server is None:
            raise NotFoundError("Сервер не найден.")
        server.max_clients = max(0, max_clients)
        await self.rebuild_user_access_matrix()
        return server

    async def get_user_servers(self, user_id: str) -> list[UserServerAccess]:
        return list(
            (
                await self.session.scalars(
                    select(UserServerAccess)
                    .where(UserServerAccess.user_id == user_id)
                    .options(selectinload(UserServerAccess.server))
                    .order_by(UserServerAccess.created_at.asc())
                )
            ).all()
        )

    async def rebuild_user_access_matrix(self) -> None:
        now = utc_now()
        users = list((await self.session.scalars(select(User))).all())
        servers = list((await self.session.scalars(select(Server).options(selectinload(Server.inbounds)))).all())
        accesses = list((await self.session.scalars(select(UserServerAccess))).all())
        access_map = {(access.user_id, access.server_id): access for access in accesses}

        active_statuses = {UserStatus.ACTIVE, UserStatus.TRIAL, UserStatus.GRACE}

        for user in users:
            for server in servers:
                desired_status = None
                if server.is_available and user.status in active_statuses:
                    desired_status = (
                        AccessStatus.GRACE if user.status == UserStatus.GRACE else AccessStatus.ACTIVE
                    )
                access = access_map.get((user.id, server.id))
                if desired_status is None:
                    if access is not None:
                        access.status = AccessStatus.BLOCKED
                        access.revoked_at = now
                        access.last_synced_at = now
                    continue
                if access is None:
                    access = UserServerAccess(
                        user_id=user.id,
                        server_id=server.id,
                        status=desired_status,
                        granted_at=now,
                        last_synced_at=now,
                    )
                    self.session.add(access)
                    access_map[(user.id, server.id)] = access
                else:
                    access.status = desired_status
                    access.revoked_at = None
                    access.last_synced_at = now
                    if access.granted_at is None:
                        access.granted_at = now

        for server in servers:
            current_clients = len(
                [
                    access
                    for access in access_map.values()
                    if access.server_id == server.id and access.status in {AccessStatus.ACTIVE, AccessStatus.GRACE}
                ]
            )
            server.current_clients = current_clients
            if server.max_clients <= 0 and current_clients > 0:
                server.max_clients = current_clients
            if server.max_clients > 0:
                server.load_percent = Decimal((current_clients / server.max_clients) * 100).quantize(
                    Decimal("0.01")
                )
            else:
                server.load_percent = Decimal("0")

            for inbound in server.inbounds:
                if inbound.is_active:
                    inbound.client_count = current_clients
                    inbound.max_clients = server.max_clients
