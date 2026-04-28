from __future__ import annotations

from decimal import Decimal

import pytest


@pytest.mark.asyncio
async def test_list_users_supports_username_and_remote_identifiers(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11001,
            username="demo_user",
            first_name="Demo",
            last_name="User",
            language_code="ru",
        )
        user.remnawave_user_uuid = "remote-user-uuid"
        user.remnawave_short_uuid = "short-uuid"
        user.remnawave_username = "remote_demo"
        user_id = user.id

    async with test_services.hub() as hub:
        assert (await hub.accounts.list_users("@demo_user"))[0].id == user_id
        assert (await hub.accounts.list_users("11001"))[0].id == user_id
        assert (await hub.accounts.list_users("remote_demo"))[0].id == user_id
        assert (await hub.accounts.list_users("remote-user-uuid"))[0].id == user_id
        assert (await hub.accounts.list_users("short-uuid"))[0].id == user_id


@pytest.mark.asyncio
async def test_complete_registration_marks_user_as_registered(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11002,
            username="registration_user",
            first_name="Registration",
            last_name="User",
            language_code="ru",
        )
        assert not hub.accounts.is_registered(user)

        await hub.accounts.complete_registration(user.id)
        updated = await hub.accounts.get_user(user.id)

        assert hub.accounts.is_registered(updated)
        assert updated.registration_completed_at is not None
        assert updated.consent_accepted_at is not None
        assert updated.consent_version == hub.accounts.registration_consent_version


@pytest.mark.asyncio
async def test_mark_channel_verified_persists_timestamp(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11003,
            username="channel_user",
            first_name="Channel",
            last_name="User",
            language_code="ru",
        )
        assert user.channel_verified_at is None

        await hub.accounts.mark_channel_verified(user.id)
        updated = await hub.accounts.get_user(user.id)

        assert updated.channel_verified_at is not None


@pytest.mark.asyncio
async def test_mark_promo_onboarding_completed_persists_timestamp(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11006,
            username="promo_onboarding_user",
            first_name="Promo",
            last_name="Onboarding",
            language_code="ru",
        )
        assert user.promo_onboarding_completed_at is None

        await hub.accounts.mark_promo_onboarding_completed(user.id)
        updated = await hub.accounts.get_user(user.id)

        assert updated.promo_onboarding_completed_at is not None


@pytest.mark.asyncio
async def test_is_trial_available_without_trial_period_returns_true(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11004,
            username="trial_free_user",
            first_name="Trial",
            last_name="Free",
            language_code="ru",
        )

        assert await hub.accounts.is_trial_available(user.id) is True


@pytest.mark.asyncio
async def test_can_offer_trial_requires_clean_account_history(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=11005,
            username="clean_user",
            first_name="Clean",
            last_name="User",
            language_code="ru",
        )

        assert await hub.accounts.can_offer_trial(user.id) is True

        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)

        assert await hub.accounts.can_offer_trial(user.id) is False


@pytest.mark.asyncio
async def test_list_admin_telegram_ids_merges_settings_and_admin_rows(test_services):
    test_services.settings.admin_allowed_telegram_ids = [101, 202]

    async with test_services.hub() as hub:
        await hub.accounts.create_admin(
            username="panel_admin",
            password="secret123",
            full_name="Panel Admin",
            telegram_id=303,
        )
        ids = await hub.accounts.list_admin_telegram_ids()

    assert ids == [101, 202, 303]
