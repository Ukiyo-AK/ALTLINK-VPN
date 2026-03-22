from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Boolean, Enum, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from altlink.domain.enums import PlanKind
from altlink.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Plan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name_ru: Mapped[str] = mapped_column(String(255))
    kind: Mapped[PlanKind] = mapped_column(
        Enum(
            PlanKind,
            native_enum=False,
            length=32,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        index=True,
    )
    price_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    duration_days: Mapped[int] = mapped_column(Integer)
    traffic_limit_bytes: Mapped[int | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    sort_order: Mapped[int] = mapped_column(Integer, default=100, server_default="100")

    subscriptions = relationship("Subscription", back_populates="plan")
