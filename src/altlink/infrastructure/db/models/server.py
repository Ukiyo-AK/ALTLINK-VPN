from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.infrastructure.db.base import Base, Json, TimestampMixin, UUIDPrimaryKeyMixin


class Server(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "servers"

    remnawave_node_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    address: Mapped[str] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(Json, nullable=True)
    active_config_profile_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)

    is_managed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", index=True)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_connected: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_disabled_remote: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    users_online: Mapped[int | None] = mapped_column(nullable=True)
    current_clients_count: Mapped[int] = mapped_column(default=0, server_default="0")
    max_clients_count: Mapped[int] = mapped_column(default=1, server_default="1")
    load_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0.00"))
    last_status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_metrics_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(Json, nullable=True)

    inbounds = relationship("ServerInbound", back_populates="server", cascade="all, delete-orphan")
    user_accesses = relationship("UserServerAccess", back_populates="server", cascade="all, delete-orphan")
    traffic_snapshots = relationship("TrafficSnapshot", back_populates="server")
    online_sessions = relationship("OnlineSessionCache", back_populates="server")
    system_events = relationship("SystemEvent", back_populates="server")

