from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.domain.enums import TopupRequestStatus
from altlink.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TopupRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "topup_requests"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[TopupRequestStatus] = mapped_column(
        Enum(
            TopupRequestStatus,
            native_enum=False,
            length=32,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        default=TopupRequestStatus.NEW,
        server_default=TopupRequestStatus.NEW.value,
        index=True,
    )
    user_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by_admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="topup_requests")
    approved_by_admin = relationship("AdminUser", back_populates="approved_topups")
    balance_transactions = relationship("BalanceTransaction", back_populates="topup_request")
