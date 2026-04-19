from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.domain.enums import AccessStatus, ServerType
from altlink.infrastructure.db.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_values

if TYPE_CHECKING:
    from altlink.infrastructure.db.models.accounts import User
    from altlink.infrastructure.db.models.ops import OnlineSessionCache, TrafficSnapshot


class Server(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "servers"

    remnawave_node_uuid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    server_type: Mapped[ServerType] = mapped_column(
        enum_values(ServerType),
        default=ServerType.REGULAR,
        nullable=False,
    )
    is_connected: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_available: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_status_change: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    users_online: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_clients: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_clients: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    load_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"), nullable=False)
    remnawave_internal_squad_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    inbounds: Mapped[list["ServerInbound"]] = relationship(back_populates="server")
    user_accesses: Mapped[list["UserServerAccess"]] = relationship(back_populates="server")
    traffic_snapshots: Mapped[list["TrafficSnapshot"]] = relationship(back_populates="server")
    online_sessions: Mapped[list["OnlineSessionCache"]] = relationship(back_populates="server")


class ServerInbound(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "server_inbounds"
    __table_args__ = (UniqueConstraint("server_id", "tag", name="uq_server_inbound_tag"),)

    server_id: Mapped[str] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    remnawave_inbound_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tag: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    network: Mapped[str | None] = mapped_column(String(64), nullable=True)
    security: Mapped[str | None] = mapped_column(String(64), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_clients: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    server: Mapped["Server"] = relationship(back_populates="inbounds")


class UserServerAccess(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_server_access"
    __table_args__ = (UniqueConstraint("user_id", "server_id", name="uq_user_server_access"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    server_id: Mapped[str] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[AccessStatus] = mapped_column(enum_values(AccessStatus), nullable=False)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="server_accesses")
    server: Mapped["Server"] = relationship(back_populates="user_accesses")
