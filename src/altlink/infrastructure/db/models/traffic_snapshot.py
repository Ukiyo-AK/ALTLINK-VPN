from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.infrastructure.db.base import Base, Json, TimestampMixin, UUIDPrimaryKeyMixin


class TrafficSnapshot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "traffic_snapshots"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subscription_id: Mapped[str | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    server_id: Mapped[str | None] = mapped_column(
        ForeignKey("servers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(64), default="remnawave")
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_bytes: Mapped[int] = mapped_column(default=0, server_default="0")
    lifetime_used_bytes: Mapped[int] = mapped_column(default=0, server_default="0")
    node_breakdown: Mapped[dict | None] = mapped_column(Json, nullable=True)

    user = relationship("User", back_populates="traffic_snapshots")
    subscription = relationship("Subscription", back_populates="traffic_snapshots")
    server = relationship("Server", back_populates="traffic_snapshots")

