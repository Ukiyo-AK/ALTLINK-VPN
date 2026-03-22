from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.domain.enums import TrialStatus
from altlink.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TrialPeriod(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "trial_periods"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    subscription_id: Mapped[str | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    activated_by_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[TrialStatus] = mapped_column(
        Enum(
            TrialStatus,
            native_enum=False,
            length=32,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        default=TrialStatus.ACTIVE,
        server_default=TrialStatus.ACTIVE.value,
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="trial_periods")
    subscription = relationship("Subscription", back_populates="trial_period")
    activated_by_admin = relationship("AdminUser", back_populates="trial_periods")
