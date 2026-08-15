from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.domain.enums import (
    BalanceTransactionType,
    PlanCode,
    PromoRewardKind,
    SubscriptionStatus,
    TopupStatus,
)
from altlink.infrastructure.db.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_values

if TYPE_CHECKING:
    from altlink.infrastructure.db.models.accounts import AdminUser, User
    from altlink.infrastructure.db.models.ops import TrafficSnapshot


class Plan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plans"

    code: Mapped[PlanCode] = mapped_column(enum_values(PlanCode), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    price_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    traffic_limit_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    device_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_trial: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="plan")


class Subscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False, index=True)
    status: Mapped[SubscriptionStatus] = mapped_column(
        enum_values(SubscriptionStatus), default=SubscriptionStatus.PENDING, nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_billing_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    billing_anchor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cycle_days_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    grace_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accrued_debt_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), nullable=False)
    traffic_limit_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    traffic_used_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    whitelist_traffic_used_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    whitelist_traffic_billed_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    whitelist_billing_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    whitelist_included_limit_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    whitelist_included_consumed_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    whitelist_usage_cursor_bytes: Mapped[int] = mapped_column(BigInteger, default=-1, nullable=False)
    whitelist_traffic_accounted_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    whitelist_notification_threshold: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_traffic_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_notification_threshold: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    auto_renew: Mapped[bool] = mapped_column(default=True, nullable=False)
    remnawave_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="subscriptions")
    plan: Mapped["Plan"] = relationship(back_populates="subscriptions")
    traffic_snapshots: Mapped[list["TrafficSnapshot"]] = relationship(back_populates="subscription")


class WhitelistPackagePurchase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "whitelist_package_purchases"
    __table_args__ = (UniqueConstraint("request_key", name="uq_whitelist_package_purchase_request"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    subscription_id: Mapped[str | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    request_key: Mapped[str] = mapped_column(String(64), nullable=False)
    package_code: Mapped[str] = mapped_column(String(16), nullable=False)
    traffic_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="completed", nullable=False)
    balance_transaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("balance_transactions.id", ondelete="SET NULL"), nullable=True
    )
    created_by_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class BalanceTransaction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "balance_transactions"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    topup_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("topup_requests.id", ondelete="SET NULL"), nullable=True
    )
    created_by_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    type: Mapped[BalanceTransactionType] = mapped_column(enum_values(BalanceTransactionType), nullable=False)
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    balance_before: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="balance_transactions")
    topup_request: Mapped["TopupRequest | None"] = relationship(back_populates="balance_transactions")
    created_by_admin: Mapped["AdminUser | None"] = relationship(back_populates="created_transactions")


class TopupRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "topup_requests"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[TopupStatus] = mapped_column(enum_values(TopupStatus), default=TopupStatus.NEW, nullable=False)
    provider_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    external_payment_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="topup_requests")
    approved_by_admin: Mapped["AdminUser | None"] = relationship(back_populates="approved_topups")
    balance_transactions: Mapped[list["BalanceTransaction"]] = relationship(back_populates="topup_request")


class TrialPeriod(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "trial_periods"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed: Mapped[bool] = mapped_column(default=True, nullable=False)
    converted_to_subscription: Mapped[bool] = mapped_column(default=False, nullable=False)

    user: Mapped["User"] = relationship(back_populates="trial_period")


class PromoCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "promo_codes"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    reward_kind: Mapped[PromoRewardKind] = mapped_column(enum_values(PromoRewardKind), nullable=False)
    reward_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    usage_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    new_users_only: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    assigned_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    created_by_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_by_admin: Mapped["AdminUser | None"] = relationship(back_populates="promo_codes")
    assigned_user: Mapped["User | None"] = relationship(
        back_populates="assigned_promo_codes",
        foreign_keys=[assigned_user_id],
    )
    redemptions: Mapped[list["PromoCodeRedemption"]] = relationship(back_populates="promo_code")


class PromoCodeRedemption(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "promo_code_redemptions"
    __table_args__ = (UniqueConstraint("promo_code_id", "user_id", name="uq_promo_code_redemption_user"),)

    promo_code_id: Mapped[str] = mapped_column(
        ForeignKey("promo_codes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    applied_subscription_id: Mapped[str | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reward_value_applied: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    promo_code: Mapped["PromoCode"] = relationship(back_populates="redemptions")
    user: Mapped["User"] = relationship(back_populates="promo_redemptions")
    applied_subscription: Mapped["Subscription | None"] = relationship()
