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
    UNLIMITED = "unlimited"
    LIMITED_50GB = "limited_50gb"


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


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class AccessStatus(StrEnum):
    ACTIVE = "active"
    GRACE = "grace"
    BLOCKED = "blocked"


class SystemEventLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

