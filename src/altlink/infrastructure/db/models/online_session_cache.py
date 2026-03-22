from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.infrastructure.db.base import Base, Json, TimestampMixin, UUIDPrimaryKeyMixin


class OnlineSessionCache(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "online_sessions_cache"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    server_id: Mapped[str | None] = mapped_column(
        ForeignKey("servers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    remnawave_node_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    request_ip: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    device_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    inbound_tag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", index=True)
    raw_data: Mapped[dict | None] = mapped_column(Json, nullable=True)

    user = relationship("User", back_populates="online_sessions")
    server = relationship("Server", back_populates="online_sessions")

