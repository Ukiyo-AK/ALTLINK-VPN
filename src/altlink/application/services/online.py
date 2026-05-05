from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import joinedload

from altlink.application.services.base import BaseService
from altlink.domain.enums import NotificationStatus, NotificationType, ServerType, SubscriptionStatus
from altlink.domain.plans import WHITELIST_GB_PRICE_RUB, is_metered_plan_code
from altlink.domain.billing import bytes_to_gb_cost
from altlink.infrastructure.db.models import Notification, OnlineSessionCache, Server, Subscription, User
from altlink.presentation.bots.common import send_telegram_messages
from altlink.utils.time import utc_now


class OnlineService(BaseService):
    source = "online"

    async def refresh_online_cache(self, detailed: bool = False) -> list[OnlineSessionCache]:
        if self.remnawave is None:
            return []

        now = utc_now()
        users = list((await self.session.scalars(select(User))).all())
        user_map = {user.remnawave_user_uuid: user for user in users if user.remnawave_user_uuid}
        telegram_map = {user.telegram_id: user for user in users}
        servers = list((await self.session.scalars(select(Server))).all())
        server_map = {server.remnawave_node_uuid: server for server in servers}
        subscriptions = list(
            (
                await self.session.scalars(
                    select(Subscription)
                    .where(
                        Subscription.status.in_(
                            [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE]
                        )
                    )
                    .options(joinedload(Subscription.plan))
                    .order_by(Subscription.created_at.desc())
                )
            ).all()
        )
        subscription_map: dict[str, Subscription] = {}
        for subscription in subscriptions:
            subscription_map.setdefault(subscription.user_id, subscription)
        previous_sessions = list(
            (
                await self.session.scalars(
                    select(OnlineSessionCache)
                    .options(joinedload(OnlineSessionCache.server))
                    .order_by(OnlineSessionCache.created_at.desc())
                )
            ).all()
        )
        previous_session_map: dict[str, OnlineSessionCache] = {}
        for session in previous_sessions:
            if session.user_id:
                previous_session_map.setdefault(session.user_id, session)

        await self.session.execute(delete(OnlineSessionCache))
        created: list[OnlineSessionCache] = []

        for remote_user in await self.remnawave.list_users():
            user = user_map.get(remote_user.uuid)
            if user is None and remote_user.telegramId is not None:
                user = telegram_map.get(remote_user.telegramId)
                if user is not None:
                    self._link_remote_user(user, remote_user)
                    user_map[user.remnawave_user_uuid] = user
            if user is None:
                continue

            server = server_map.get(remote_user.userTraffic.lastConnectedNodeUuid or "")
            ip_address = None
            last_agent = remote_user.subLastUserAgent
            history_summary: dict | None = None
            if detailed:
                history = await self.remnawave.get_subscription_request_history(remote_user.uuid)
                if history:
                    latest = history[0]
                    ip_address = latest.requestIp
                    last_agent = latest.userAgent or last_agent
                    history_summary = self._summarize_history(history)

            payload = remote_user.model_dump(mode="json")
            if history_summary:
                payload["historySummary"] = history_summary

            session = OnlineSessionCache(
                user_id=user.id,
                server_id=server.id if server else None,
                user=user,
                server=server,
                remote_ip=ip_address,
                user_agent=last_agent,
                device=self.describe_device(last_agent),
                inbound=None,
                last_activity_at=remote_user.userTraffic.onlineAt,
                is_online=bool(
                    remote_user.userTraffic.onlineAt
                    and remote_user.userTraffic.onlineAt >= now - timedelta(minutes=2)
                ),
                raw_payload=payload,
            )
            self.session.add(session)
            created.append(session)
            subscription = subscription_map.get(user.id)
            previous_session = previous_session_map.get(user.id)
            if self._should_notify_whitelist_connection(session, previous_session, subscription):
                await self._queue_whitelist_connection_notice(user, session, subscription)

        return created

    def _link_remote_user(self, user: User, remote_user) -> None:
        user.remnawave_user_uuid = remote_user.uuid
        user.remnawave_username = remote_user.username
        user.remnawave_short_uuid = remote_user.shortUuid

    async def list_online(self, only_online: bool = True) -> list[OnlineSessionCache]:
        query = (
            select(OnlineSessionCache)
            .options(joinedload(OnlineSessionCache.user), joinedload(OnlineSessionCache.server))
            .order_by(OnlineSessionCache.last_activity_at.desc().nullslast())
        )
        if only_online:
            query = query.where(OnlineSessionCache.is_online.is_(True))
        return list((await self.session.scalars(query.limit(200))).all())

    async def get_user_activity_summary(self, user_id: str) -> dict | None:
        session = await self.session.scalar(
            select(OnlineSessionCache)
            .where(OnlineSessionCache.user_id == user_id)
            .options(joinedload(OnlineSessionCache.server))
            .order_by(OnlineSessionCache.last_activity_at.desc().nullslast(), OnlineSessionCache.created_at.desc())
            .limit(1)
        )
        if session is None:
            return None
        return self.session_summary(session)

    def session_summary(self, session: OnlineSessionCache) -> dict:
        raw_payload = session.raw_payload or {}
        history = raw_payload.get("historySummary") or {}
        current_device = session.device or history.get("current_device") or self.describe_device(session.user_agent)
        recent_devices = history.get("recent_devices") or []
        recent_ips = history.get("recent_ips") or []
        return {
            "current_status": "online" if session.is_online else "offline",
            "current_device": current_device,
            "current_server_type": session.server.server_type.value if session.server else None,
            "recent_server_types": [session.server.server_type.value] if session.server else [],
            "unique_device_count": int(history.get("unique_device_count") or (1 if current_device else 0)),
            "unique_ip_count": int(history.get("unique_ip_count") or (1 if session.remote_ip else 0)),
            "last_ip": session.remote_ip,
            "last_seen_at": session.last_activity_at.isoformat() if session.last_activity_at else None,
            "recent_devices": recent_devices[:4],
            "recent_ips": recent_ips[:4],
        }

    def _should_notify_whitelist_connection(
        self,
        session: OnlineSessionCache,
        previous_session: OnlineSessionCache | None,
        subscription: Subscription | None,
    ) -> bool:
        if (
            not session.is_online
            or session.server is None
            or session.server.server_type != ServerType.WHITELIST
            or subscription is None
            or subscription.plan is None
        ):
            return False
        if (
            previous_session is not None
            and previous_session.is_online
            and previous_session.server is not None
            and previous_session.server.server_type == ServerType.WHITELIST
        ):
            return False
        return True

    async def _queue_whitelist_connection_notice(
        self,
        user: User,
        session: OnlineSessionCache,
        subscription: Subscription,
    ) -> None:
        if subscription.plan is None:
            return
        last_activity = session.last_activity_at or getattr(user, "last_seen_at", None) or utc_now()
        server_marker = session.server_id or "unknown"
        dedupe_key = f"whitelist-online:{user.id}:{server_marker}:{last_activity.isoformat()}"
        existing = await self.session.scalar(select(Notification).where(Notification.dedupe_key == dedupe_key))
        if existing is not None:
            return

        message = self._whitelist_connection_notice_text(user, subscription)
        notification = Notification(
            user_id=user.id,
            type=NotificationType.BROADCAST,
            message=message,
            dedupe_key=dedupe_key,
        )
        if self.settings.client_bot_token and user.telegram_id:
            delivered = await send_telegram_messages(
                bot_token=self.settings.client_bot_token,
                chat_ids=[user.telegram_id],
                text=message,
            )
            if delivered > 0:
                notification.status = NotificationStatus.SENT
                notification.sent_at = datetime.now().astimezone()
            else:
                notification.failure_reason = "Immediate whitelist delivery failed; queued for retry."
        self.session.add(notification)

    def _whitelist_connection_notice_text(self, user: User, subscription: Subscription) -> str:
        if subscription.plan and is_metered_plan_code(subscription.plan.code):
            return (
                "⚠️ Вы подключились к серверу белых списков.\n\n"
                "Такие серверы медленнее обычных и рекомендуются только в ситуациях, когда мобильный интернет работает по белым спискам.\n\n"
                f"Текущий баланс: {Decimal(user.balance_rub):.2f} ₽\n"
                f"Трафик белых списков: {subscription.whitelist_traffic_used_bytes / 1024**3:.2f} ГБ\n"
                f"Уже списано за белые списки: {bytes_to_gb_cost(subscription.whitelist_traffic_billed_bytes, WHITELIST_GB_PRICE_RUB):.2f} ₽\n\n"
                "❗ ВАЖНО: ТРАФИК ПО БЕЛЫМ СПИСКАМ СПИСЫВАЕТСЯ С БАЛАНСА СРАЗУ ПО 4 ₽ ЗА 1 ГБ.\n"
                "Баланс может уйти в минус максимум до -50 ₽."
            )
        return (
            "⚠️ Вы подключились к серверу белых списков.\n\n"
            "Такие серверы работают медленнее обычных. Настоятельно рекомендуем использовать их только в ситуациях, когда мобильный интернет работает по белым спискам."
        )

    @staticmethod
    def describe_device(user_agent: str | None) -> str:
        if not user_agent:
            return "устройство не определено"
        normalized = user_agent.lower()
        client_map = [
            ("shadowrocket", "Shadowrocket"),
            ("streisand", "Streisand"),
            ("hiddify", "Hiddify"),
            ("nekobox", "NekoBox"),
            ("v2rayn", "v2rayN"),
            ("clash", "Clash"),
            ("sing-box", "sing-box"),
            ("sfa", "SFA"),
            ("outline", "Outline"),
        ]
        platform_map = [
            ("iphone", "iPhone"),
            ("ipad", "iPad"),
            ("ios", "iOS"),
            ("android", "Android"),
            ("windows", "Windows"),
            ("mac os", "macOS"),
            ("macos", "macOS"),
            ("linux", "Linux"),
        ]
        client = next((label for marker, label in client_map if marker in normalized), None)
        platform = next((label for marker, label in platform_map if marker in normalized), None)
        if client and platform:
            return f"{client} • {platform}"
        if client:
            return client
        if platform:
            return platform
        return user_agent[:80]

    def _summarize_history(self, history) -> dict:
        device_rows: list[dict[str, str | None]] = []
        device_counter: Counter[str] = Counter()
        ip_counter: Counter[str] = Counter()

        for record in history:
            device_label = self.describe_device(record.userAgent)
            request_ip = record.requestIp or None
            device_counter[device_label] += 1
            if request_ip:
                ip_counter[request_ip] += 1
            device_rows.append(
                {
                    "label": device_label,
                    "ip": request_ip,
                    "request_at": record.requestAt.isoformat(),
                }
            )

        recent_devices: list[dict[str, object]] = []
        seen_devices: set[str] = set()
        for row in device_rows:
            label = str(row["label"])
            if label in seen_devices:
                continue
            seen_devices.add(label)
            recent_devices.append(
                {
                    "label": label,
                    "ip": row["ip"],
                    "last_seen_at": row["request_at"],
                    "hits": device_counter[label],
                }
            )
            if len(recent_devices) >= 4:
                break

        recent_ips: list[dict[str, object]] = []
        seen_ips: set[str] = set()
        for row in device_rows:
            ip = row["ip"]
            if not ip or ip in seen_ips:
                continue
            seen_ips.add(ip)
            recent_ips.append(
                {
                    "ip": ip,
                    "last_seen_at": row["request_at"],
                    "hits": ip_counter[ip],
                }
            )
            if len(recent_ips) >= 4:
                break

        return {
            "current_device": recent_devices[0]["label"] if recent_devices else "устройство не определено",
            "unique_device_count": len(device_counter),
            "unique_ip_count": len(ip_counter),
            "recent_devices": recent_devices,
            "recent_ips": recent_ips,
        }
