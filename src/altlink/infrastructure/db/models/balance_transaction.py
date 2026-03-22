from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.domain.enums import BalanceTransactionType
from altlink.infrastructure.db.base import Base, Json, TimestampMixin, UUIDPrimaryKeyMixin


class BalanceTransaction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "balance_transactions"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subscription_id: Mapped[str | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    topup_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("topup_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )
    admin_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    transaction_type: Mapped[BalanceTransactionType] = mapped_column(
        Enum(
            BalanceTransactionType,
            native_enum=False,
            length=32,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        index=True,
    )
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    balance_before: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(Json, nullable=True)

    user = relationship("User", back_populates="balance_transactions")
    subscription = relationship("Subscription", back_populates="balance_transactions")
    topup_request = relationship("TopupRequest", back_populates="balance_transactions")
    admin_user = relationship("AdminUser", back_populates="transactions")
