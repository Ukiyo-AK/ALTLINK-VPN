from __future__ import annotations

from enum import StrEnum


class UserStatus(StrEnum):
    NEW = "new"
    TRIAL = "trial"
    ACTIVE = "active"
    GRACE = "grace"
    BLOCKED = "blocked"
    CANCELED = "canceled"


class SubscriptionStatus(StrEnum):
    PENDING = "pending"
    TRIAL = "trial"
    ACTIVE = "active"
    GRACE = "grace"
    BLOCKED = "blocked"
    CANCELED = "canceled"
    EXPIRED = "expired"


class PlanCode(StrEnum):
    TRIAL = "trial"
    SINGLE_10GBIT = "single_10gbit"
    SINGLE_10GBIT_WEEKLY = "single_10gbit_weekly"
    UNLIMITED = "unlimited"
    UNLIMITED_WEEKLY = "unlimited_weekly"


class ServerType(StrEnum):
    TEN_GBIT = "ten_gbit"
    WHITELIST = "whitelist"
    REGULAR = "regular"


class TopupStatus(StrEnum):
    NEW = "new"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELED = "canceled"


class BalanceTransactionType(StrEnum):
    TOPUP = "topup"
    SUBSCRIPTION_CHARGE = "subscription_charge"
    MANUAL_ADJUSTMENT = "manual_adjustment"
    REFUND = "refund"
    PROMO_BONUS = "promo_bonus"
    PROMO_APPLIED = "promo_applied"
    REFERRAL_BONUS = "referral_bonus"


class TrafficLimitStrategy(StrEnum):
    NO_RESET = "NO_RESET"
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"


class NotificationType(StrEnum):
    LOW_BALANCE = "low_balance"
    UPCOMING_RENEWAL = "upcoming_renewal"
    GRACE_STARTED = "grace_started"
    GRACE_REMINDER = "grace_reminder"
    ACCESS_BLOCKED = "access_blocked"
    TOPUP_APPROVED = "topup_approved"
    TOPUP_REJECTED = "topup_rejected"
    TRAFFIC_THRESHOLD = "traffic_threshold"
    TRAFFIC_EXCEEDED = "traffic_exceeded"
    TRIAL_ENDED = "trial_ended"
    PROMO_CODE = "promo_code"
    REFERRAL_BONUS = "referral_bonus"
    BROADCAST = "broadcast"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class SupportRequestStatus(StrEnum):
    NEW = "new"
    RESOLVED = "resolved"


class PromoRewardKind(StrEnum):
    BALANCE = "balance"
    PLAN_DISCOUNT = "plan_discount"
    REPEAT_TRIAL = "repeat_trial"


class AccessStatus(StrEnum):
    ACTIVE = "active"
    GRACE = "grace"
    BLOCKED = "blocked"


class SystemEventLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
