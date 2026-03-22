from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class AdminLoginForm(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=255)


class BalanceAdjustmentForm(BaseModel):
    amount_rub: Decimal
    comment: str = Field(min_length=2, max_length=500)


class TopupDecisionForm(BaseModel):
    comment: str | None = Field(default=None, max_length=500)


class ServerToggleForm(BaseModel):
    is_enabled: bool
    max_clients_count: int = Field(ge=1, le=100000)


class UserSearchForm(BaseModel):
    query: str = Field(min_length=1, max_length=255)

