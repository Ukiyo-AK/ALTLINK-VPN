from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.domain.enums import UserServerAccessStatus
from altlink.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UserServerAccess(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_server_access"
    __table_args__ = (UniqueConstraint("user_id", "server_id"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    server_id: Mapped[str] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    status: Mapped[UserServerAccessStatus] = mapped_column(
        Enum(
            UserServerAccessStatus,
            native_enum=False,
            length=32,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        default=UserServerAccessStatus.ACTIVE,
        server_default=UserServerAccessStatus.ACTIVE.value,
        index=True,
    )
    config_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="server_accesses")
    server = relationship("Server", back_populates="user_accesses")
