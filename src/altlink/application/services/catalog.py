from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from altlink.application.services.base import BaseService, NotFoundError
from altlink.domain.enums import AccessStatus, PlanCode, ServerType, SubscriptionStatus, SystemEventLevel, UserStatus
from altlink.infrastructure.db.models import Server, ServerInbound, Subscription, User, UserServerAccess
from altlink.utils.time import utc_now


class CatalogService(BaseService):
    source = "catalog"

    async def list_servers(self) -> list[Server]:
        return list(
            (
                await self.session.scalars(
                    select(Server).options(selectinload(Server.inbounds)).order_by(Server.server_type.asc(), Server.name.asc())
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
            existing_inbounds: list[ServerInbound] = []
            if server is None:
                server = Server(
                    remnawave_node_uuid=node.uuid,
                    name=node.name,
                    address=node.address,
                    is_available=True,
                    server_type=ServerType.REGULAR,
                )
                self.session.add(server)
                await self.session.flush()
            else:
                existing_inbounds = list(server.inbounds)

            server.name = node.name
            server.address = node.address
            server.country_code = node.countryCode
            server.is_connected = node.isConnected
            server.last_status_message = node.lastStatusMessage
            server.last_status_change = node.lastStatusChange
            server.users_online = node.usersOnline or 0
            server.raw_payload = node.model_dump(mode="json")
            server.last_sync_at = now

            inbound_map = {inbound.tag: inbound for inbound in existing_inbounds}
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

            for inbound in existing_inbounds:
                if inbound.tag not in active_tags:
                    inbound.is_active = False

        for key, server in current_servers.items():
            if key not in seen_node_ids:
                server.is_connected = False
                server.last_sync_at = now

        await self._sync_internal_squads()
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

    async def set_server_type(self, server_id: str, server_type: ServerType) -> Server:
        server = await self.session.get(Server, server_id, options=[selectinload(Server.inbounds)])
        if server is None:
            raise NotFoundError("Сервер не найден.")
        server.server_type = server_type
        await self._sync_internal_squads([server])
        await self.rebuild_user_access_matrix()
        return server

    async def assign_preferred_server(self, user_id: str) -> Server:
        user = await self.session.get(User, user_id, options=[joinedload(User.assigned_server)])
        if user is None:
            raise NotFoundError("Пользователь не найден.")
        server = await self._pick_least_loaded_ten_gbit_server()
        user.assigned_server_id = server.id
        await self.session.flush()
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
        users = list(
            (
                await self.session.scalars(
                    select(User)
                    .options(
                        joinedload(User.assigned_server),
                        selectinload(User.subscriptions).joinedload(Subscription.plan),
                    )
                )
            ).all()
        )
        servers = list((await self.session.scalars(select(Server).options(selectinload(Server.inbounds)))).all())
        accesses = list((await self.session.scalars(select(UserServerAccess))).all())
        access_map = {(access.user_id, access.server_id): access for access in accesses}

        active_statuses = {UserStatus.ACTIVE, UserStatus.TRIAL, UserStatus.GRACE}
        user_server_targets: dict[str, set[str]] = {}

        for user in users:
            subscription = self._resolve_current_subscription(user.subscriptions)
            desired_server_ids = set()
            if user.status in active_statuses and subscription is not None:
                desired_server_ids = await self._resolve_server_targets(user, subscription, servers)
            user_server_targets[user.id] = desired_server_ids

            for server in servers:
                desired_status = None
                if server.id in desired_server_ids and server.is_available:
                    desired_status = AccessStatus.GRACE if user.status == UserStatus.GRACE else AccessStatus.ACTIVE
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

        await self._sync_user_squads(users, user_server_targets)

    async def _resolve_server_targets(
        self,
        user: User,
        subscription: Subscription,
        servers: Sequence[Server],
    ) -> set[str]:
        available_servers = [server for server in servers if server.is_available and self._server_has_active_inbounds(server)]
        if subscription.plan.code == PlanCode.UNLIMITED:
            return {server.id for server in available_servers}

        if subscription.plan.code in {PlanCode.TRIAL, PlanCode.SINGLE_10GBIT}:
            assigned_server = next((server for server in available_servers if server.id == user.assigned_server_id), None)
            if assigned_server is None or assigned_server.server_type != ServerType.TEN_GBIT:
                assigned_server = await self._pick_least_loaded_ten_gbit_server(servers)
                user.assigned_server_id = assigned_server.id
            desired_server_ids = {assigned_server.id}
            if subscription.plan.code == PlanCode.SINGLE_10GBIT:
                desired_server_ids.update(
                    server.id for server in available_servers if server.server_type == ServerType.WHITELIST
                )
            return desired_server_ids

        return set()

    async def _pick_least_loaded_ten_gbit_server(self, servers: Sequence[Server] | None = None) -> Server:
        if servers is None:
            servers = list((await self.session.scalars(select(Server).options(selectinload(Server.inbounds)))).all())
        candidates = [
            server
            for server in servers
            if server.is_available
            and server.server_type == ServerType.TEN_GBIT
            and self._server_has_active_inbounds(server)
        ]
        if not candidates:
            raise NotFoundError("Нет доступного 10 Гбит сервера для назначения.")
        candidates.sort(key=lambda item: (item.load_percent, item.current_clients, item.name.lower()))
        return candidates[0]

    async def _sync_internal_squads(self, servers: Sequence[Server] | None = None) -> None:
        if self.remnawave is None:
            return

        target_servers = list(servers) if servers is not None else await self.list_servers()
        remote_squads = {squad.uuid: squad for squad in await self.remnawave.list_internal_squads()}
        remote_by_name = {squad.name: squad for squad in remote_squads.values()}

        for server in target_servers:
            inbound_ids = [
                inbound.remnawave_inbound_uuid
                for inbound in server.inbounds
                if inbound.is_active and inbound.remnawave_inbound_uuid
            ]
            if not inbound_ids:
                continue

            squad_name = self._squad_name(server)
            squad = None
            if server.remnawave_internal_squad_uuid:
                squad = remote_squads.get(server.remnawave_internal_squad_uuid)
            if squad is None:
                squad = remote_by_name.get(squad_name)

            if squad is None:
                created = await self.remnawave.create_internal_squad(name=squad_name, inbounds=inbound_ids)
                server.remnawave_internal_squad_uuid = created.uuid
            else:
                updated = await self.remnawave.update_internal_squad(
                    squad_uuid=squad.uuid,
                    name=squad_name,
                    inbounds=inbound_ids,
                )
                server.remnawave_internal_squad_uuid = updated.uuid

    async def _sync_user_squads(self, users: Sequence[User], user_server_targets: dict[str, set[str]]) -> None:
        if self.remnawave is None:
            return

        server_map = {
            server.id: server
            for server in (
                await self.session.scalars(select(Server).options(selectinload(Server.inbounds)))
            ).all()
        }
        for user in users:
            subscription = self._resolve_current_subscription(user.subscriptions)
            if not user.remnawave_user_uuid or subscription is None or subscription.plan is None:
                continue
            squad_ids = [
                server_map[server_id].remnawave_internal_squad_uuid
                for server_id in sorted(user_server_targets.get(user.id, set()))
                if server_id in server_map and server_map[server_id].remnawave_internal_squad_uuid
            ]
            expire_at = subscription.grace_until if subscription.status == SubscriptionStatus.GRACE else subscription.ends_at
            await self.remnawave.update_user(
                {
                    "uuid": user.remnawave_user_uuid,
                    "username": user.remnawave_username or f"tg_{user.telegram_id}",
                    "status": "ACTIVE" if user.status in {UserStatus.ACTIVE, UserStatus.TRIAL, UserStatus.GRACE} else "DISABLED",
                    "expireAt": expire_at.isoformat(),
                    "trafficLimitBytes": int(subscription.traffic_limit_bytes or 0),
                    "trafficLimitStrategy": "NO_RESET",
                    "telegramId": user.telegram_id,
                    "description": f"ALTLINK user {user.telegram_id}",
                    "activeInternalSquads": squad_ids,
                }
            )

    def _resolve_current_subscription(self, subscriptions: Sequence[Subscription]) -> Subscription | None:
        active_states = {SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE}
        candidates = [item for item in subscriptions if item.status in active_states and item.plan is not None]
        candidates.sort(key=lambda item: item.created_at, reverse=True)
        return candidates[0] if candidates else None

    def _server_has_active_inbounds(self, server: Server) -> bool:
        return any(inbound.is_active and inbound.remnawave_inbound_uuid for inbound in server.inbounds)

    def _squad_name(self, server: Server) -> str:
        safe_name = "".join(ch if ch.isalnum() or ch in {" ", "-", "_"} else "_" for ch in server.name).strip()
        safe_name = safe_name or "Server"
        return f"ALTLINK {server.id[:6]} {safe_name}"[:30]
