from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from altlink.application.services.base import ServiceBase
from altlink.application.services.notifications import NotificationService
from altlink.domain.enums import NotificationType, SubscriptionStatus, UserStatus
from altlink.infrastructure.db.models import OnlineSessionCache, Server, Subscription, TrafficSnapshot, User


class TrafficService(ServiceBase):
    async def sync_active_user_traffic(self) -> int:
        if self.remnawave is None:
            raise RuntimeError("Remnawave client is required")

        now = datetime.now(UTC)
        result = await self.session.execute(
            select(User, Subscription)
            .join(Subscription, Subscription.user_id == User.id)
            .where(
                Subscription.is_current.is_(True),
                Subscription.status.in_(
                    [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE]
                ),
                User.remnawave_user_uuid.is_not(None),
            )
        )
        notifier = NotificationService(self.session, self.settings, self.remnawave)
        processed = 0
        for user, subscription in result.all():
            remote_user = await self.remnawave.get_user_by_uuid(user.remnawave_user_uuid)
            snapshot = TrafficSnapshot(
                user_id=user.id,
                subscription_id=subscription.id,
                snapshot_at=now,
                period_start=subscription.current_period_start,
                period_end=subscription.current_period_end,
                used_bytes=remote_user.userTraffic.usedTrafficBytes,
                lifetime_used_bytes=remote_user.userTraffic.lifetimeUsedTrafficBytes,
            )
            self.session.add(snapshot)
            subscription.traffic_used_bytes_cache = remote_user.userTraffic.usedTrafficBytes
            user.last_remnawave_sync_at = now
            processed += 1
            if (
                subscription.traffic_limit_bytes_snapshot
                and subscription.traffic_limit_bytes_snapshot > 0
                and subscription.status in {SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE}
            ):
                percent = remote_user.userTraffic.usedTrafficBytes / subscription.traffic_limit_bytes_snapshot * 100
                for threshold in self.settings.traffic_notify_thresholds:
                    if percent >= threshold:
                        if threshold >= 100:
                            if user.status != UserStatus.BLOCKED:
                                subscription.status = SubscriptionStatus.BLOCKED
                                subscription.blocked_at = now
                                user.status = UserStatus.BLOCKED
                                if user.remnawave_user_uuid:
                                    await self.remnawave.disable_user(user.remnawave_user_uuid)
                                await notifier.queue(
                                    user=user,
                                    notification_type=NotificationType.TRAFFIC_LIMIT_REACHED,
                                    title="Лимит трафика исчерпан",
                                    message="Вы использовали весь месячный лимит трафика. Доступ будет восстановлен после нового оплаченного периода.",
                                    dedupe_key=f"traffic-limit:{subscription.id}:{subscription.current_period_start.date().isoformat()}",
                                )
                        else:
                            await notifier.queue(
                                user=user,
                                notification_type=NotificationType.TRAFFIC_WARNING,
                                title="Трафик почти закончился",
                                message=f"Вы использовали уже {int(percent)}% месячного лимита трафика.",
                                dedupe_key=f"traffic-warning:{subscription.id}:{subscription.current_period_start.date().isoformat()}:{threshold}",
                            )
        return processed

    async def sync_online_sessions(self) -> int:
        if self.remnawave is None:
            raise RuntimeError("Remnawave client is required")

        await self.session.execute(delete(OnlineSessionCache))
        result = await self.session.execute(
            select(User, Subscription)
            .join(Subscription, Subscription.user_id == User.id)
            .where(
                Subscription.is_current.is_(True),
                Subscription.status.in_(
                    [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE]
                ),
                User.remnawave_user_uuid.is_not(None),
            )
        )
        count = 0
        now = datetime.now(UTC)
        server_map = {server.remnawave_node_uuid: server for server in (await self.session.execute(select(Server))).scalars()}
        for user, _subscription in result.all():
            remote_user = await self.remnawave.get_user_by_uuid(user.remnawave_user_uuid)
            history = await self.remnawave.get_user_subscription_request_history(user.remnawave_user_uuid)
            last_record = history.records[0] if history.records else None
            node_uuid = remote_user.userTraffic.lastConnectedNodeUuid
            server = server_map.get(node_uuid) if node_uuid else None
            last_seen = remote_user.userTraffic.onlineAt or (last_record.requestAt if last_record else None)
            self.session.add(
                OnlineSessionCache(
                    user_id=user.id,
                    server_id=server.id if server else None,
                    remnawave_node_uuid=node_uuid,
                    request_ip=last_record.requestIp if last_record else None,
                    user_agent=last_record.userAgent if last_record else None,
                    device_hint=(last_record.userAgent[:120] if last_record and last_record.userAgent else None),
                    inbound_tag=None,
                    last_activity_at=last_seen,
                    observed_at=now,
                    is_online=bool(last_seen and last_seen >= now - timedelta(minutes=5)),
                    raw_data={
                        "history_total": history.total,
                        "last_connected_node_uuid": node_uuid,
                    },
                )
            )
            count += 1
        return count

    async def get_usage_summary_for_user(self, user: User) -> dict:
        subscription = await self.get_current_subscription(user.id)
        if subscription is None:
            return {"subscription": None, "usage": None}
        if self.remnawave is None or user.remnawave_user_uuid is None:
            return {"subscription": subscription, "usage": None}
        usage = await self.remnawave.get_user_usage(
            user.remnawave_user_uuid,
            start=(subscription.current_period_start or datetime.now(UTC)).date(),
            end=(subscription.current_period_end or datetime.now(UTC)).date(),
            top_nodes_limit=10,
        )
        return {"subscription": subscription, "usage": usage}
