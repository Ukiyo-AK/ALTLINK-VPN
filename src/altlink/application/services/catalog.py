from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload, selectinload

from altlink.application.services.base import BaseService, ConflictError, NotFoundError, ServiceError
from altlink.domain.enums import AccessStatus, PlanCode, ServerType, SubscriptionStatus, SystemEventLevel, UserStatus
from altlink.domain.plans import (
    START_WHITELIST_BALANCE_FLOOR_RUB,
    WHITELIST_BILLING_VERSION,
    is_metered_plan_code,
    is_unlimited_plan_code,
)
from altlink.domain.traffic_limits import effective_traffic_limit
from altlink.infrastructure.db.models import (
    OnlineSessionCache,
    Server,
    ServerInbound,
    Subscription,
    TrafficSnapshot,
    User,
    UserServerAccess,
)
from altlink.utils.time import ensure_utc, utc_now

logger = logging.getLogger(__name__)


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
        created_servers: list[dict[str, str | None]] = []
        updated_servers: list[dict[str, str | None]] = []
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
                created_servers.append({"node_uuid": node.uuid, "name": node.name, "address": node.address})
            else:
                existing_inbounds = list(server.inbounds)
                updated_servers.append({"node_uuid": node.uuid, "name": node.name, "address": node.address})

            server.name = node.name
            server.address = node.address
            server.country_code = node.countryCode
            server.is_connected = bool(node.isConnected and not node.isDisabled)
            server.last_status_message = node.lastStatusMessage
            server.last_status_change = node.lastStatusChange
            server.users_online = node.usersOnline or 0
            server.raw_payload = node.model_dump(mode="json")
            server.last_sync_at = now

            inbound_map = {inbound.tag: inbound for inbound in existing_inbounds}
            active_tags: set[str] = set()
            duplicate_tags: set[str] = set()
            for remote_inbound in node.configProfile.activeInbounds:
                if remote_inbound.tag in active_tags:
                    duplicate_tags.add(remote_inbound.tag)
                active_tags.add(remote_inbound.tag)
                inbound = inbound_map.get(remote_inbound.tag)
                if inbound is None:
                    inbound = ServerInbound(
                        server_id=server.id,
                        tag=remote_inbound.tag,
                        type=remote_inbound.type,
                        access_type=ServerType.REGULAR.value,
                    )
                    self.session.add(inbound)
                    inbound_map[remote_inbound.tag] = inbound
                if not inbound.access_type:
                    inbound.access_type = ServerType.REGULAR.value
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
            if duplicate_tags:
                logger.warning(
                    "Remnawave node %s (%s) returned duplicate active inbound tags: %s",
                    node.name,
                    node.uuid,
                    sorted(duplicate_tags),
                )

        removed_servers: list[dict] = []
        for key, server in current_servers.items():
            if key not in seen_node_ids:
                removed_servers.append(await self._purge_server(server))

        try:
            await self._sync_internal_squads()
        except SQLAlchemyError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("Failed to sync Remnawave internal squads after server catalog sync.", exc_info=True)
            await self.log_event(
                level=SystemEventLevel.WARNING,
                event_type="server_internal_squads_sync_failed",
                message="Список серверов обновлён, но internal squads Remnawave синхронизировать не удалось.",
            )
        await self.rebuild_user_access_matrix()
        if removed_servers:
            await self.log_event(
                level=SystemEventLevel.WARNING,
                event_type="servers_removed_from_sync",
                message="Пропавшие в Remnawave серверы удалены из локальной базы.",
                payload={"count": len(removed_servers), "servers": removed_servers},
            )
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="servers_synced",
            message="Список серверов синхронизирован с Remnawave.",
            payload={
                "count": len(remote_nodes),
                "created_count": len(created_servers),
                "updated_count": len(updated_servers),
                "removed_count": len(removed_servers),
                "created_servers": created_servers,
            },
        )
        return await self.list_servers()

    async def refresh_server_health(self) -> dict:
        """Refresh node health without changing a user's pinned Start server."""
        if self.remnawave is None:
            return {
                "checked": 0,
                "changed_servers": [],
                "affected_start_users": [],
                "start_failovers": [],
                "skipped": "remnawave_not_configured",
            }

        remote_nodes = await self.remnawave.list_nodes()
        if not remote_nodes:
            # An empty panel response is more likely a transient/partial API failure than
            # every node disappearing at once. The full catalog sync will reconcile removals.
            logger.warning("Skipping server failover because Remnawave returned an empty node list.")
            return {
                "checked": 0,
                "changed_servers": [],
                "affected_start_users": [],
                "start_failovers": [],
                "skipped": "empty_remote_catalog",
            }

        now = utc_now()
        remote_by_uuid = {node.uuid: node for node in remote_nodes}
        servers = list(
            (
                await self.session.scalars(
                    select(Server).options(selectinload(Server.inbounds))
                )
            ).all()
        )
        changed_servers: list[dict] = []
        unusable_start_server_ids: set[str] = set()

        for server in servers:
            was_usable = self._server_is_usable(server)
            remote = remote_by_uuid.get(server.remnawave_node_uuid)
            if remote is None:
                server.is_connected = False
                server.last_status_message = "Нода отсутствует в текущем ответе Remnawave."
                server.last_status_change = now
            else:
                server.is_connected = bool(remote.isConnected and not remote.isDisabled)
                server.last_status_message = remote.lastStatusMessage
                server.last_status_change = remote.lastStatusChange
                server.users_online = remote.usersOnline or 0
                server.raw_payload = remote.model_dump(mode="json")
                server.last_sync_at = now

            is_usable = self._server_is_usable(server)
            if server.server_type == ServerType.TEN_GBIT and not is_usable:
                unusable_start_server_ids.add(server.id)
            if was_usable != is_usable:
                changed_servers.append(
                    {
                        "server_id": server.id,
                        "name": server.name,
                        "was_usable": was_usable,
                        "is_usable": is_usable,
                    }
                )

        affected_start_users = []
        if unusable_start_server_ids:
            candidates = list(
                (
                    await self.session.scalars(
                        select(User)
                        .where(
                            User.assigned_server_id.in_(unusable_start_server_ids),
                            User.status.in_([UserStatus.ACTIVE, UserStatus.GRACE]),
                        )
                        .options(selectinload(User.subscriptions).joinedload(Subscription.plan))
                    )
                ).all()
            )
            for user in candidates:
                subscription = self._resolve_current_subscription(user.subscriptions)
                if subscription and subscription.plan and is_metered_plan_code(subscription.plan.code):
                    affected_start_users.append(
                        {
                            "user_id": user.id,
                            "telegram_id": user.telegram_id,
                            "server_id": user.assigned_server_id,
                        }
                    )

        if changed_servers:
            await self.rebuild_user_access_matrix()
            await self.session.flush()

        if changed_servers:
            await self.log_event(
                level=SystemEventLevel.WARNING,
                event_type="server_health_changed",
                message="Состояние серверов изменилось. Start-серверы пользователей автоматически не менялись.",
                payload={
                    "changed_servers": changed_servers,
                    "affected_start_users": affected_start_users,
                },
            )

        return {
            "checked": len(servers),
            "changed_servers": changed_servers,
            "affected_start_users": affected_start_users,
            "start_failovers": [],
            "skipped": None,
        }

    async def refresh_server_health_and_failover(self) -> dict:
        """Backward-compatible alias for deployments that still call the old method name."""
        return await self.refresh_server_health()

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
        # Тип сервера влияет только на локальную маршрутизацию тарифов и матрицу доступов.
        # Remnawave internal squads завязаны на inbound'ы ноды, а не на TEN_GBIT/REGULAR/WHITELIST.
        # Поэтому смена типа не должна зависеть от отдельного internal-squad API панели.
        await self.rebuild_user_access_matrix()
        return server

    async def force_delete_server(self, server_id: str) -> dict:
        server = await self.session.get(Server, server_id, options=[selectinload(Server.inbounds)])
        if server is None:
            raise NotFoundError("Сервер не найден.")
        summary = await self._purge_server(server)

        await self.rebuild_user_access_matrix()
        await self.log_event(
            level=SystemEventLevel.WARNING,
            event_type="server_force_deleted",
            message="Сервер принудительно удалён из локальной базы.",
            payload=summary,
        )
        return summary

    async def _purge_server(self, server: Server) -> dict:
        server_id = server.id
        summary = {
            "server_id": server.id,
            "name": server.name,
            "address": server.address,
            "remnawave_node_uuid": server.remnawave_node_uuid,
            "remnawave_internal_squad_uuid": server.remnawave_internal_squad_uuid,
            "assigned_users": await self._count_where(User, User.assigned_server_id == server_id),
            "accesses": await self._count_where(UserServerAccess, UserServerAccess.server_id == server_id),
            "inbounds": await self._count_where(ServerInbound, ServerInbound.server_id == server_id),
            "traffic_snapshots": await self._count_where(TrafficSnapshot, TrafficSnapshot.server_id == server_id),
            "online_sessions": await self._count_where(OnlineSessionCache, OnlineSessionCache.server_id == server_id),
        }

        await self.session.execute(
            update(User).where(User.assigned_server_id == server_id).values(assigned_server_id=None)
        )
        await self.session.execute(
            update(TrafficSnapshot).where(TrafficSnapshot.server_id == server_id).values(server_id=None)
        )
        await self.session.execute(
            update(OnlineSessionCache).where(OnlineSessionCache.server_id == server_id).values(server_id=None)
        )
        await self.session.execute(delete(UserServerAccess).where(UserServerAccess.server_id == server_id))
        await self.session.execute(delete(ServerInbound).where(ServerInbound.server_id == server_id))
        await self.session.execute(delete(Server).where(Server.id == server_id))
        await self.session.flush()
        return summary

    async def assign_preferred_server(self, user_id: str, plan_code: PlanCode | None = None) -> Server:
        user = await self.session.get(User, user_id, options=[joinedload(User.assigned_server)])
        if user is None:
            raise NotFoundError("Пользователь не найден.")
        if is_metered_plan_code(plan_code) and user.assigned_server_id:
            assigned_server = await self.session.get(
                Server,
                user.assigned_server_id,
                options=[selectinload(Server.inbounds)],
            )
            if assigned_server is not None and assigned_server.server_type == ServerType.TEN_GBIT:
                return assigned_server
        server = await self._pick_preferred_server_for_plan(plan_code)
        user.assigned_server_id = server.id
        await self.session.flush()
        return server

    async def list_available_start_servers(self) -> list[Server]:
        servers = await self.list_servers()
        available = [
            server
            for server in servers
            if server.server_type == ServerType.TEN_GBIT and self._server_is_usable(server)
        ]
        available.sort(key=self._server_assignment_sort_key)
        return available

    async def reassign_start_server(
        self,
        user_id: str,
        server_id: str,
        *,
        admin_id: str | None = None,
    ) -> Server:
        user = await self.session.get(User, user_id, options=[joinedload(User.assigned_server)])
        if user is None:
            raise NotFoundError("Пользователь не найден.")
        subscription = await self.session.scalar(
            select(Subscription)
            .where(
                Subscription.user_id == user.id,
                Subscription.status.in_(
                    [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE]
                ),
            )
            .options(joinedload(Subscription.plan))
            .order_by(Subscription.created_at.desc())
        )
        if (
            subscription is None
            or subscription.plan is None
            or not is_metered_plan_code(subscription.plan.code)
        ):
            raise ConflictError("Ручное переназначение доступно только пользователям тарифа Start.")
        expire_at = (
            subscription.grace_until
            if subscription.status == SubscriptionStatus.GRACE and subscription.grace_until is not None
            else subscription.ends_at
        )
        if ensure_utc(expire_at) <= utc_now():
            raise ConflictError("Подписка Start уже истекла. Сначала восстановите доступ пользователя.")

        server = await self.get_server(server_id)
        if server.server_type != ServerType.TEN_GBIT:
            raise ConflictError("Можно выбрать только сервер типа Start.")
        if not self._server_is_usable(server):
            raise ConflictError("Выбранный Start-сервер сейчас недоступен.")

        previous_server_id = user.assigned_server_id
        user.assigned_server_id = server.id
        try:
            await self.rebuild_user_access_matrix()
            await self.session.flush()
            if self.remnawave is not None and user.remnawave_user_uuid:
                remote_user = await self.remnawave.get_user(user.remnawave_user_uuid)
                remote_squad_ids = {
                    squad.uuid for squad in (remote_user.activeInternalSquads or [])
                }
                if (
                    not server.remnawave_internal_squad_uuid
                    or server.remnawave_internal_squad_uuid not in remote_squad_ids
                ):
                    raise ConflictError("Remnawave не подтвердила назначение выбранного Start-сервера.")
        except Exception as exc:
            user.assigned_server_id = previous_server_id
            await self.rebuild_user_access_matrix()
            await self.session.flush()
            if isinstance(exc, ServiceError):
                raise
            if isinstance(exc, httpx.HTTPError):
                raise ConflictError("Не удалось подтвердить назначение сервера в Remnawave.") from exc
            raise

        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="start_server_manually_reassigned",
            message="Администратор вручную переназначил Start-сервер пользователя.",
            payload={
                "user_id": user.id,
                "telegram_id": user.telegram_id,
                "from_server_id": previous_server_id,
                "to_server_id": server.id,
                "to_server_name": server.name,
            },
            actor_admin_id=admin_id,
            subject_user_id=user.id,
        )
        return server

    async def get_user_servers(self, user_id: str, *, active_only: bool = True) -> list[UserServerAccess]:
        query = (
            select(UserServerAccess)
            .where(UserServerAccess.user_id == user_id)
            .options(selectinload(UserServerAccess.server).selectinload(Server.inbounds))
            .order_by(UserServerAccess.created_at.asc())
        )
        if active_only:
            query = query.where(UserServerAccess.status.in_([AccessStatus.ACTIVE, AccessStatus.GRACE]))
        return list(
            (
                await self.session.scalars(query)
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
                if server.id in desired_server_ids and self._server_is_usable(server):
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
            online_clients = max(int(server.users_online or 0), 0)
            if server.max_clients > 0:
                server.load_percent = Decimal((online_clients / server.max_clients) * 100).quantize(
                    Decimal("0.01")
                )
            else:
                server.load_percent = Decimal("0")

            for inbound in server.inbounds:
                if inbound.is_active:
                    inbound.client_count = current_clients
                    inbound.max_clients = server.max_clients

        await self._sync_user_squads(users, user_server_targets)

    async def sync_user_target_squads(self, user_id: str) -> None:
        if self.remnawave is None:
            return
        accesses = await self.get_user_servers(user_id)
        target_server_ids = {
            access.server_id
            for access in accesses
            if access.server_id and access.server and self._server_is_usable(access.server)
        }
        if not target_server_ids:
            return
        servers = [
            server
            for server in await self.list_servers()
            if server.id in target_server_ids
        ]
        await self._sync_internal_squads(servers, strict=True)

    async def _resolve_server_targets(
        self,
        user: User,
        subscription: Subscription,
        servers: Sequence[Server],
    ) -> set[str]:
        available_servers = [server for server in servers if self._server_is_usable(server)]
        if subscription.plan.code == PlanCode.TRIAL:
            return {server.id for server in available_servers}

        versioned_whitelist = int(subscription.whitelist_billing_version or 1) >= WHITELIST_BILLING_VERSION
        if versioned_whitelist:
            included_remaining = max(
                int(subscription.whitelist_included_limit_bytes or 0)
                - int(subscription.whitelist_included_consumed_bytes or 0),
                0,
            )
            whitelist_allowed = bool(
                included_remaining
                or int(user.whitelist_extra_traffic_bytes or 0) > 0
                or Decimal(user.balance_rub) > 0
            )
        else:
            whitelist_allowed = Decimal(user.balance_rub) > START_WHITELIST_BALANCE_FLOOR_RUB

        if is_unlimited_plan_code(subscription.plan.code):
            return {
                server.id
                for server in available_servers
                if server.server_type != ServerType.WHITELIST or whitelist_allowed or not versioned_whitelist
            }

        if is_metered_plan_code(subscription.plan.code):
            desired_server_ids = (
                {server.id for server in available_servers if server.server_type == ServerType.WHITELIST}
                if whitelist_allowed
                else set()
            )
            assigned_server = next((server for server in servers if server.id == user.assigned_server_id), None)
            if (
                assigned_server is None
                or assigned_server.server_type != ServerType.TEN_GBIT
                or not self._server_is_usable(assigned_server)
            ):
                return desired_server_ids
            desired_server_ids.add(assigned_server.id)
            return desired_server_ids

        return set()

    async def _pick_least_loaded_ten_gbit_server(self, servers: Sequence[Server] | None = None) -> Server:
        if servers is None:
            servers = list((await self.session.scalars(select(Server).options(selectinload(Server.inbounds)))).all())
        candidates = [
            server
            for server in servers
            if server.server_type == ServerType.TEN_GBIT
            and self._server_is_usable(server)
        ]
        if not candidates:
            raise NotFoundError("Нет доступного 10 Гбит сервера для назначения.")
        candidates.sort(key=self._server_assignment_sort_key)
        return candidates[0]

    async def _pick_preferred_server_for_plan(
        self,
        plan_code: PlanCode | None,
        servers: Sequence[Server] | None = None,
    ) -> Server:
        if servers is None:
            servers = list((await self.session.scalars(select(Server).options(selectinload(Server.inbounds)))).all())

        if plan_code == PlanCode.TRIAL or is_unlimited_plan_code(plan_code):
            candidates = [server for server in servers if self._server_is_usable(server)]
            if not candidates:
                raise NotFoundError("Нет доступных серверов для активации Pro доступа.")
            candidates.sort(key=self._server_assignment_sort_key)
            return candidates[0]

        return await self._pick_least_loaded_ten_gbit_server(servers)

    async def _sync_internal_squads(self, servers: Sequence[Server] | None = None, *, strict: bool = False) -> None:
        if self.remnawave is None:
            return

        target_servers = list(servers) if servers is not None else await self.list_servers()
        remote_squads = {squad.uuid: squad for squad in await self.remnawave.list_internal_squads()}
        remote_by_name = {squad.name: squad for squad in remote_squads.values()}

        for server in target_servers:
            if not self._server_is_usable(server):
                continue
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
                try:
                    created = await self.remnawave.create_internal_squad(name=squad_name, inbounds=inbound_ids)
                    server.remnawave_internal_squad_uuid = created.uuid
                except Exception as exc:  # noqa: BLE001
                    message = self._format_internal_squad_sync_error(
                        "создать",
                        server=server,
                        squad_name=squad_name,
                        inbound_ids=inbound_ids,
                        exc=exc,
                    )
                    logger.warning(message, exc_info=True)
                    if strict:
                        raise ConflictError(message) from exc
                    continue
            else:
                try:
                    updated = await self.remnawave.update_internal_squad(
                        squad_uuid=squad.uuid,
                        name=squad_name,
                        inbounds=inbound_ids,
                    )
                    server.remnawave_internal_squad_uuid = updated.uuid
                except Exception as exc:  # noqa: BLE001
                    message = self._format_internal_squad_sync_error(
                        "обновить",
                        server=server,
                        squad_name=squad_name,
                        inbound_ids=inbound_ids,
                        exc=exc,
                        remote_squad_uuid=squad.uuid,
                    )
                    logger.warning(message, exc_info=True)
                    if strict:
                        raise ConflictError(message) from exc
                    continue

    async def _sync_user_squads(self, users: Sequence[User], user_server_targets: dict[str, set[str]]) -> None:
        if self.remnawave is None:
            return

        server_map = {
            server.id: server
            for server in (
                await self.session.scalars(select(Server).options(selectinload(Server.inbounds)))
            ).all()
        }
        now = utc_now()
        for user in users:
            subscription = self._resolve_current_subscription(user.subscriptions)
            if not user.remnawave_user_uuid or subscription is None or subscription.plan is None:
                continue
            squad_ids = [
                server.remnawave_internal_squad_uuid
                for server_id in sorted(user_server_targets.get(user.id, set()))
                if (server := server_map.get(server_id)) is not None
                and self._server_is_usable(server)
                and server.remnawave_internal_squad_uuid
            ]
            expire_at = subscription.grace_until if subscription.status == SubscriptionStatus.GRACE else subscription.ends_at
            traffic_limit_bytes, traffic_limit_strategy = effective_traffic_limit(user, subscription)
            if ensure_utc(expire_at) <= now:
                logger.warning(
                    "Skipping remote squad sync for expired subscription %s of user %s.",
                    subscription.id,
                    user.id,
                )
                continue
            try:
                await self.remnawave.update_user(
                    {
                        "uuid": user.remnawave_user_uuid,
                        "username": user.remnawave_username or f"tg_{user.telegram_id}",
                        "status": "ACTIVE" if user.status in {UserStatus.ACTIVE, UserStatus.TRIAL, UserStatus.GRACE} else "DISABLED",
                        "expireAt": expire_at.isoformat(),
                        "trafficLimitBytes": traffic_limit_bytes,
                        "trafficLimitStrategy": traffic_limit_strategy.value,
                        "hwidDeviceLimit": subscription.plan.device_limit,
                        "telegramId": user.telegram_id,
                        "description": f"ALTLINK user {user.telegram_id}",
                        "activeInternalSquads": squad_ids,
                    }
                )
            except httpx.HTTPError:
                logger.warning("Failed to sync remote squads for user %s", user.id, exc_info=True)
                continue
            except Exception:  # noqa: BLE001
                logger.warning("Unexpected failure while syncing remote squads for user %s", user.id, exc_info=True)
                continue

    def _resolve_current_subscription(self, subscriptions: Sequence[Subscription]) -> Subscription | None:
        active_states = {SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE}
        candidates = [item for item in subscriptions if item.status in active_states and item.plan is not None]
        candidates.sort(key=lambda item: item.created_at, reverse=True)
        return candidates[0] if candidates else None

    async def _count_where(self, model, condition) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(model).where(condition)) or 0)

    def _server_is_usable(self, server: Server) -> bool:
        return bool(server.is_available and server.is_connected and self._server_has_active_inbounds(server))

    def is_server_usable(self, server: Server | None) -> bool:
        return bool(server and self._server_is_usable(server))

    def _server_has_active_inbounds(self, server: Server) -> bool:
        return any(inbound.is_active and inbound.remnawave_inbound_uuid for inbound in server.inbounds)

    def _server_assignment_sort_key(self, server: Server) -> tuple[Decimal, int, Decimal, str]:
        return (
            Decimal(getattr(server, "current_clients", 0) or 0),
            int(getattr(server, "users_online", 0) or 0),
            Decimal(getattr(server, "load_percent", 0) or 0),
            server.name.lower(),
        )

    def _squad_name(self, server: Server) -> str:
        safe_name = "".join(ch if ch.isalnum() or ch in {" ", "-", "_"} else "_" for ch in server.name).strip()
        safe_name = safe_name or "Server"
        return f"ALTLINK {server.id[:6]} {safe_name}"[:30]

    def _format_internal_squad_sync_error(
        self,
        action: str,
        *,
        server: Server,
        squad_name: str,
        inbound_ids: Sequence[str],
        exc: Exception,
        remote_squad_uuid: str | None = None,
    ) -> str:
        reason = self._format_remnawave_exception(exc)
        current_squad_uuid = remote_squad_uuid or server.remnawave_internal_squad_uuid or "—"
        inbounds = ", ".join(inbound_ids) if inbound_ids else "—"
        return (
            f"Не удалось {action} internal squad в Remnawave. "
            f"Сервер: {server.name}; "
            f"адрес: {server.address or '—'}; "
            f"локальный server_id: {server.id}; "
            f"node_uuid: {server.remnawave_node_uuid}; "
            f"squad_name: {squad_name}; "
            f"squad_uuid: {current_squad_uuid}; "
            f"inbounds: {inbounds}. "
            f"Причина: {reason}"
        )

    def _format_remnawave_exception(self, exc: Exception) -> str:
        if not isinstance(exc, httpx.HTTPStatusError):
            return str(exc) or exc.__class__.__name__

        response = exc.response
        try:
            payload = response.json()
        except ValueError:
            payload = None

        errors: list[str] = []
        if isinstance(payload, dict):
            details = payload.get("errors")
            if isinstance(details, list):
                for item in details:
                    if isinstance(item, dict):
                        message = item.get("message") or item.get("code")
                        path = item.get("path")
                        if isinstance(path, list) and path:
                            message = f"{'.'.join(str(part) for part in path)}: {message}"
                        if message:
                            errors.append(str(message))
            message = payload.get("message")
            if message and not errors:
                errors.append(str(message))

        detail = "; ".join(errors) if errors else response.text[:300]
        return f"Remnawave отклонил операцию: {detail}"
