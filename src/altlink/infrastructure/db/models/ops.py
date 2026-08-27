from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.domain.enums import NotificationStatus, NotificationType, SupportRequestStatus, SystemEventLevel
from altlink.infrastructure.db.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_values

if TYPE_CHECKING:
    from altlink.infrastructure.db.models.accounts import AdminUser, User
    from altlink.infrastructure.db.models.billing import Subscription
    from altlink.infrastructure.db.models.catalog import Server


class TrafficSnapshot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "traffic_snapshots"
    __table_args__ = (
        Index("ix_traffic_snapshots_user_server_created", "user_id", "server_id", "created_at"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    subscription_id: Mapped[str | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    server_id: Mapped[str | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"), nullable=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lifetime_used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)

    user: Mapped["User"] = relationship(back_populates="traffic_snapshots")
    subscription: Mapped["Subscription | None"] = relationship(back_populates="traffic_snapshots")
    server: Mapped["Server | None"] = relationship(back_populates="traffic_snapshots")


class ServerMetricSnapshot(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "server_metric_snapshots"
    __table_args__ = (
        Index("ix_server_metric_snapshots_server_captured", "server_id", "captured_at"),
        Index("ix_server_metric_snapshots_captured_at", "captured_at"),
    )

    server_id: Mapped[str] = mapped_column(String(36), nullable=False)
    remnawave_node_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    server_name: Mapped[str] = mapped_column(String(255), nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    server_type: Mapped[str] = mapped_column(String(32), nullable=False)
    is_operational: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_connected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    assigned_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    online_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    xray_uptime: Mapped[str | None] = mapped_column(String(64), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notifications"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    type: Mapped[NotificationType] = mapped_column(enum_values(NotificationType), nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        enum_values(NotificationStatus), default=NotificationStatus.PENDING, nullable=False
    )
    channel: Mapped[str] = mapped_column(String(32), default="telegram", nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="notifications")


class SupportRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "support_requests"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    resolved_by_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[SupportRequestStatus] = mapped_column(
        enum_values(SupportRequestStatus), default=SupportRequestStatus.NEW, nullable=False
    )
    topic: Mapped[str] = mapped_column(String(64), default="vpn_issue", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="support_requests")
    resolved_by_admin: Mapped["AdminUser | None"] = relationship(back_populates="resolved_support_requests")
    messages: Mapped[list["SupportMessage"]] = relationship(
        back_populates="support_request",
        cascade="all, delete-orphan",
        order_by="SupportMessage.created_at.asc()",
    )


class SupportMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "support_messages"

    support_request_id: Mapped[str] = mapped_column(
        ForeignKey("support_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sender_type: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    attachment_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attachment_mime_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attachment_original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attachment_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    support_request: Mapped["SupportRequest"] = relationship(back_populates="messages")
    user: Mapped["User | None"] = relationship(back_populates="support_messages")
    admin: Mapped["AdminUser | None"] = relationship(back_populates="support_messages")


class OnlineSessionCache(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "online_sessions_cache"

    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    server_id: Mapped[str | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"), nullable=True, index=True)
    remote_ip: Mapped[str | None] = mapped_column(String(128), nullable=True)
    device: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    inbound: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    user: Mapped["User | None"] = relationship(back_populates="online_sessions")
    server: Mapped["Server | None"] = relationship(back_populates="online_sessions")


class PortalLoginAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "portal_login_attempts"

    token: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    approved_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    approved_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    approved_user: Mapped["User | None"] = relationship(foreign_keys=[approved_user_id])


class SystemSetting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    value: Mapped[dict | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )

    updated_by_admin: Mapped["AdminUser | None"] = relationship(back_populates="updated_settings")


class SystemEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "system_events"

    subject_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    actor_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    level: Mapped[SystemEventLevel] = mapped_column(enum_values(SystemEventLevel), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    actor_admin: Mapped["AdminUser | None"] = relationship(back_populates="system_events")
