from decimal import Decimal
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from altlink.application.services.base import ConflictError
from altlink.application.services.billing import DEFAULT_PROMO_CAMPAIGN_SETTINGS
from altlink.domain.enums import PlanCode, SubscriptionStatus, TopupStatus
from altlink.infrastructure.db.models import TrialPeriod
from altlink.utils.time import utc_now


async def new_user(hub, telegram_id=662201):
    return await hub.accounts.get_or_create_user(
        telegram_id=telegram_id, username=None, first_name="Test", last_name=None, language_code="ru",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(TopupStatus))
async def test_payment_attempt_does_not_remove_trial_gift(test_services, status):
    async with test_services.hub() as hub:
        user = await new_user(hub)
        request = await hub.topups.create_request(user.id, Decimal("100"), auto_complete=False)
        request.status = status
        assert await hub.accounts.can_offer_trial(user.id)
        await hub.billing.activate_trial(user.id, user_requested=True)
        assert not await hub.accounts.can_offer_trial(user.id)


@pytest.mark.asyncio
async def test_unaffordable_plan_does_not_consume_trial(test_services):
    async with test_services.hub() as hub:
        user = await new_user(hub)
        quote = await hub.billing.quote_paid_plan_activation(user.id, PlanCode.UNLIMITED)
        assert quote.required_topup_rub > 0
        with pytest.raises(ConflictError):
            await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED)
        assert await hub.accounts.can_offer_trial(user.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("paid", [False, True])
async def test_failed_activation_keeps_trial_gift_and_retries_cleanly(test_services, monkeypatch, paid):
    async with test_services.hub() as hub:
        user = await new_user(hub)
        user.balance_rub = Decimal("1000")
        original = test_services.remnawave.create_user
        monkeypatch.setattr(test_services.remnawave, "create_user", AsyncMock(side_effect=httpx.ConnectError("offline")))
        with pytest.raises(ConflictError):
            if paid:
                await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED)
            else:
                await hub.billing.activate_trial(user.id)
        assert await hub.accounts.can_offer_trial(user.id)
        assert await hub.accounts.get_current_subscription(user.id) is None
        assert user.balance_rub == Decimal("1000")
        monkeypatch.setattr(test_services.remnawave, "create_user", original)
        await hub.billing.activate_trial(user.id)
        assert not await hub.accounts.can_offer_trial(user.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("subscription_status", [SubscriptionStatus.ACTIVE, SubscriptionStatus.BLOCKED, SubscriptionStatus.CANCELED])
async def test_gift_is_gone_after_actual_paid_activation(test_services, subscription_status):
    async with test_services.hub() as hub:
        user = await new_user(hub)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED, charge_user=False)
        subscription.status = subscription_status
        assert not await hub.accounts.can_offer_trial(user.id)


@pytest.mark.asyncio
async def test_trial_sync_only_updates_recipient_and_preserves_server_totals(test_services, monkeypatch):
    async with test_services.hub() as hub:
        first = await new_user(hub)
        await hub.billing.activate_trial(first.id)
        first_id = first.id
        original_access = {
            access.server_id: access.last_synced_at for access in await hub.catalog.get_user_servers(first_id)
        }
        second = await new_user(hub, 662202)
        second_id = second.id

    update_remote = AsyncMock(wraps=test_services.remnawave.update_user)
    create_remote = AsyncMock(wraps=test_services.remnawave.create_user)
    monkeypatch.setattr(test_services.remnawave, "update_user", update_remote)
    monkeypatch.setattr(test_services.remnawave, "create_user", create_remote)
    async with test_services.hub() as hub:
        await hub.billing.activate_trial(second_id)
        first_access = await hub.catalog.get_user_servers(first_id)
        second_access = await hub.catalog.get_user_servers(second_id)
        assert {item.server_id for item in second_access} == set(original_access)
        assert original_access
        for access in first_access:
            assert access.last_synced_at == original_access[access.server_id]
        servers = await hub.catalog.list_servers()
        for server in servers:
            if server.id in original_access:
                assert server.current_clients == 2
                assert all(inbound.client_count == 2 for inbound in server.inbounds if inbound.is_active)
        second = await hub.accounts.get_user(second_id)
        remote = test_services.remnawave.users[second.remnawave_user_uuid]
        assert {squad.uuid for squad in remote.activeInternalSquads} == {
            item.server.remnawave_internal_squad_uuid for item in second_access
        }
    update_remote.assert_not_awaited()
    create_remote.assert_awaited_once()
    assert create_remote.await_args.args[0]["telegramId"] == 662202


@pytest.mark.asyncio
@pytest.mark.parametrize("history,inactive_days,enabled,delay,allowed", [
    ("none", 0, False, 30, True),
    ("paid", 1, True, 30, False),
    ("paid", 40, True, 30, True),
    ("paid", 40, False, 30, False),
    ("paid", 40, True, 60, False),
    ("trial", 40, True, 60, False),
    ("trial", 40, True, 30, True),
])
async def test_public_trial_checks_winback_rules(test_services, monkeypatch, history, inactive_days, enabled, delay, allowed):
    async with test_services.hub() as hub:
        user = await new_user(hub)
        if history != "none":
            subscription = (
                await hub.billing.activate_trial(user.id) if history == "trial"
                else await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED, charge_user=False)
            )
            subscription.status = SubscriptionStatus.BLOCKED
            subscription.ends_at = utc_now() - timedelta(days=inactive_days)
            subscription.blocked_at = subscription.ends_at
            if history == "trial":
                trial = await hub.session.scalar(select(TrialPeriod).where(TrialPeriod.user_id == user.id))
                trial.ends_at = subscription.ends_at
        monkeypatch.setattr(hub.billing, "_promo_campaign_settings", AsyncMock(return_value={
            **DEFAULT_PROMO_CAMPAIGN_SETTINGS,
            "return_trial_enabled": enabled, "deep_winback_delay_days": delay,
        }))
        if allowed:
            assert (await hub.billing.activate_trial(user.id, user_requested=True)).status == SubscriptionStatus.TRIAL
        else:
            with pytest.raises(ConflictError):
                await hub.billing.activate_trial(user.id, user_requested=True)
            assert await hub.accounts.get_current_subscription(user.id) is None


@pytest.mark.asyncio
async def test_paid_winback_uses_latest_purchase_not_old_longer_plan(test_services):
    async with test_services.hub() as hub:
        user = await new_user(hub)
        older = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED, charge_user=False)
        newer = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED_WEEKLY, charge_user=False)
        now = utc_now()
        older.created_at = now - timedelta(days=50)
        older.status = SubscriptionStatus.CANCELED
        older.canceled_at = now - timedelta(days=40)
        older.ends_at = now + timedelta(days=1)
        newer.created_at = now - timedelta(days=8)
        newer.status = SubscriptionStatus.BLOCKED
        newer.ends_at = now - timedelta(days=1)
        newer.blocked_at = newer.ends_at
        with pytest.raises(ConflictError):
            await hub.billing.activate_trial(user.id, user_requested=True)
