from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.domain.enums import SubscriptionStatus
from altlink.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Subscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), index=True)
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(
            SubscriptionStatus,
            native_enum=False,
            length=32,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        default=SubscriptionStatus.PENDING,
        server_default=SubscriptionStatus.PENDING.value,
        index=True,
    )
    is_current: Mapped[bool] = mapped_column(default=True, server_default="true", index=True)
    is_trial: Mapped[bool] = mapped_column(default=False, server_default="false", index=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_billing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    grace_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grace_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_traffic_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    renewal_price_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    debt_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    traffic_limit_bytes_snapshot: Mapped[int | None] = mapped_column(nullable=True)
    traffic_used_bytes_cache: Mapped[int] = mapped_column(default=0, server_default="0")
    grace_speed_limit_mbps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    user = relationship("User", back_populates="subscriptions")
    plan = relationship("Plan", back_populates="subscriptions")
    balance_transactions = relationship("BalanceTransaction", back_populates="subscription")
    traffic_snapshots = relationship("TrafficSnapshot", back_populates="subscription")
    system_events = relationship("SystemEvent", back_populates="subscription")
    trial_period = relationship("TrialPeriod", back_populates="subscription", uselist=False)
