from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class TelegramRegistrationPayload(BaseModel):
    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None


class CreateTopupForm(BaseModel):
    amount_rub: Decimal = Field(gt=0)
    comment: str | None = Field(default=None, max_length=500)


class PlanSelectionForm(BaseModel):
    plan_code: str

