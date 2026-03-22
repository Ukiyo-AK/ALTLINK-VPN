from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.infrastructure.db.base import Base, Json, TimestampMixin, UUIDPrimaryKeyMixin


class ServerInbound(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "server_inbounds"
    __table_args__ = (UniqueConstraint("server_id", "remnawave_inbound_uuid"),)

    server_id: Mapped[str] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    remnawave_inbound_uuid: Mapped[str] = mapped_column(String(36), index=True)
    config_profile_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    config_profile_inbound_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tag: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(64))
    network: Mapped[str | None] = mapped_column(String(64), nullable=True)
    security: Mapped[str | None] = mapped_column(String(64), nullable=True)
    port: Mapped[int | None] = mapped_column(nullable=True)
    active_squads: Mapped[list[str] | None] = mapped_column(Json, nullable=True)
    current_clients_count: Mapped[int] = mapped_column(default=0, server_default="0")
    raw_inbound: Mapped[dict | None] = mapped_column(Json, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    server = relationship("Server", back_populates="inbounds")

