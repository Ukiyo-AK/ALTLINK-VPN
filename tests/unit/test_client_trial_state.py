from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from altlink.presentation.bots import client_handlers


@pytest.mark.asyncio
async def test_show_home_syncs_trial_state_before_render(monkeypatch):
    user = SimpleNamespace(id="user-42", balance_rub=Decimal("0"))
    target = SimpleNamespace(from_user=SimpleNamespace(id=42))
    send_home_card = AsyncMock()

    async def fake_ensure_user(from_user, container, hub):
        return user

    monkeypatch.setattr(client_handlers, "ensure_user", fake_ensure_user)
    monkeypatch.setattr(client_handlers, "send_home_card", send_home_card)

    hub = SimpleNamespace(
        billing=SimpleNamespace(sync_user_trial_state=AsyncMock()),
        accounts=SimpleNamespace(
            get_current_subscription=AsyncMock(return_value=None),
            get_latest_subscription=AsyncMock(return_value=None),
            can_offer_trial=AsyncMock(return_value=True),
        ),
    )
    container = SimpleNamespace(settings=SimpleNamespace())

    await client_handlers.show_home(target, container, hub)

    hub.billing.sync_user_trial_state.assert_awaited_once_with("user-42")
    send_home_card.assert_awaited_once()
