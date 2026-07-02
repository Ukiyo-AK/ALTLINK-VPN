from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.domain.enums import UserStatus
from altlink.infrastructure.db.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_values

if TYPE_CHECKING:
    from altlink.infrastructure.db.models.billing import (
        BalanceTransaction,
        PromoCode,
        PromoCodeRedemption,
        Subscription,
        TopupRequest,
        TrialPeriod,
    )
    from altlink.infrastructure.db.models.catalog import Server, UserServerAccess
    from altlink.infrastructure.db.models.ops import (
        Notification,
        OnlineSessionCache,
        SupportMessage,
        SupportRequest,
        SystemEvent,
        SystemSetting,
        TrafficSnapshot,
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    balance_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), nullable=False)
    status: Mapped[UserStatus] = mapped_column(enum_values(UserStatus), default=UserStatus.NEW, nullable=False)
    registration_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promo_onboarding_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    referral_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    referred_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    referral_reward_granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vless_keys_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    remnawave_user_uuid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    remnawave_username: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    remnawave_short_uuid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    assigned_server_id: Mapped[str | None] = mapped_column(
        ForeignKey("servers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user")
    balance_transactions: Mapped[list["BalanceTransaction"]] = relationship(back_populates="user")
    topup_requests: Mapped[list["TopupRequest"]] = relationship(back_populates="user")
    trial_period: Mapped["TrialPeriod | None"] = relationship(back_populates="user", uselist=False)
    promo_redemptions: Mapped[list["PromoCodeRedemption"]] = relationship(back_populates="user")
    assigned_promo_codes: Mapped[list["PromoCode"]] = relationship(
        back_populates="assigned_user",
        foreign_keys="PromoCode.assigned_user_id",
    )
    notifications: Mapped[list["Notification"]] = relationship(back_populates="user")
    support_requests: Mapped[list["SupportRequest"]] = relationship(back_populates="user")
    support_messages: Mapped[list["SupportMessage"]] = relationship(back_populates="user")
    server_accesses: Mapped[list["UserServerAccess"]] = relationship(back_populates="user")
    online_sessions: Mapped[list["OnlineSessionCache"]] = relationship(back_populates="user")
    traffic_snapshots: Mapped[list["TrafficSnapshot"]] = relationship(back_populates="user")
    assigned_server: Mapped["Server | None"] = relationship(foreign_keys=[assigned_server_id])
    referred_by_user: Mapped["User | None"] = relationship(remote_side="User.id", foreign_keys=[referred_by_user_id])


class AdminUser(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "admin_users"

    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    approved_topups: Mapped[list["TopupRequest"]] = relationship(back_populates="approved_by_admin")
    created_transactions: Mapped[list["BalanceTransaction"]] = relationship(back_populates="created_by_admin")
    promo_codes: Mapped[list["PromoCode"]] = relationship(back_populates="created_by_admin")
    updated_settings: Mapped[list["SystemSetting"]] = relationship(back_populates="updated_by_admin")
    system_events: Mapped[list["SystemEvent"]] = relationship(back_populates="actor_admin")
    resolved_support_requests: Mapped[list["SupportRequest"]] = relationship(back_populates="resolved_by_admin")
    support_messages: Mapped[list["SupportMessage"]] = relationship(back_populates="admin")
