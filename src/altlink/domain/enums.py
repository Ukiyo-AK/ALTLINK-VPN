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


class PlanKind(StrEnum):
    TRIAL = "trial"
    UNLIMITED = "unlimited"
    LIMITED = "limited"


class TopupRequestStatus(StrEnum):
    NEW = "new"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELED = "canceled"


class BalanceTransactionType(StrEnum):
    TOPUP = "topup"
    RENEWAL = "renewal"
    MANUAL_ADJUSTMENT = "manual_adjustment"
    TRIAL_ACTIVATION = "trial_activation"
    DEBT_SETTLEMENT = "debt_settlement"
    REFUND = "refund"


class NotificationType(StrEnum):
    LOW_BALANCE = "low_balance"
    UPCOMING_RENEWAL = "upcoming_renewal"
    GRACE_STARTED = "grace_started"
    GRACE_REMINDER = "grace_reminder"
    ACCESS_BLOCKED = "access_blocked"
    TOPUP_APPROVED = "topup_approved"
    TOPUP_REJECTED = "topup_rejected"
    TRAFFIC_WARNING = "traffic_warning"
    TRAFFIC_LIMIT_REACHED = "traffic_limit_reached"
    TRIAL_ENDING = "trial_ending"
    TRIAL_ENDED = "trial_ended"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    CANCELED = "canceled"


class TrialStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELED = "canceled"


class UserServerAccessStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"
    REMOVED = "removed"
    UNAVAILABLE = "unavailable"


class EventLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

