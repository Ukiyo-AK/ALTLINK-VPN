from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.domain.enums import UserStatus
from altlink.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)

    status: Mapped[UserStatus] = mapped_column(
        Enum(
            UserStatus,
            native_enum=False,
            length=32,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        default=UserStatus.NEW,
        server_default=UserStatus.NEW.value,
        index=True,
    )
    balance_rub: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), server_default="0.00"
    )
    is_manual_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    remnawave_user_uuid: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    remnawave_username: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    remnawave_short_uuid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    remnawave_subscription_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_bot_interaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_remnawave_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan")
    balance_transactions = relationship(
        "BalanceTransaction", back_populates="user", cascade="all, delete-orphan"
    )
    topup_requests = relationship("TopupRequest", back_populates="user", cascade="all, delete-orphan")
    traffic_snapshots = relationship(
        "TrafficSnapshot", back_populates="user", cascade="all, delete-orphan"
    )
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")
    trial_periods = relationship("TrialPeriod", back_populates="user", cascade="all, delete-orphan")
    server_accesses = relationship(
        "UserServerAccess", back_populates="user", cascade="all, delete-orphan"
    )
    online_sessions = relationship(
        "OnlineSessionCache", back_populates="user", cascade="all, delete-orphan"
    )
