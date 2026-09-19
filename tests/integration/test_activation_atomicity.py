from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from altlink.application.services.base import ConflictError
from altlink.domain.enums import PlanCode
from altlink.infrastructure.db.models import BalanceTransaction, Subscription, TrialPeriod


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["trial", "paid", "switch"])
async def test_failed_activation_is_atomic_when_ui_catches_error(test_services, monkeypatch, scenario):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=770001, username="activation_failure", first_name="Test",
            last_name="User", language_code="ru",
        )
        user_id = user.id
        user.balance_rub = Decimal("1000")
        if scenario == "switch":
            await hub.billing.activate_paid_plan(user_id, PlanCode.UNLIMITED)
        balance_before = user.balance_rub
        state_before = user.status
        current = await hub.accounts.get_current_subscription(user_id)
        current_id = current.id if current else None
        transaction_count = await hub.session.scalar(select(func.count()).select_from(BalanceTransaction))

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user(user_id)
        monkeypatch.setattr(hub.billing, "_sync_user_remote_access", AsyncMock(side_effect=ConflictError("Panel unavailable")))
        # Bot and web handlers catch service errors before the hub commits.
        with pytest.raises(ConflictError, match="Panel unavailable"):
            if scenario == "trial":
                await hub.billing.activate_trial(user_id)
            else:
                code = PlanCode.SINGLE_10GBIT if scenario == "switch" else PlanCode.UNLIMITED
                await hub.billing.activate_paid_plan(user_id, code)
        assert user.id == user_id

    async with test_services.hub() as hub:
        user = await hub.accounts.get_user(user_id)
        current = await hub.accounts.get_current_subscription(user_id)
        assert user.balance_rub == balance_before
        assert user.status == state_before
        assert (current.id if current else None) == current_id
        assert await hub.session.scalar(select(func.count()).select_from(BalanceTransaction)) == transaction_count
        assert await hub.session.scalar(select(func.count()).select_from(Subscription)) == (1 if scenario == "switch" else 0)
        assert await hub.session.scalar(select(func.count()).select_from(TrialPeriod)) == 0
