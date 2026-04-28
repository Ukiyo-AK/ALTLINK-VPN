from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_portal_auth_attempt_can_be_created_approved_and_consumed(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=31001,
            username="portal_user",
            first_name="Portal",
            last_name="User",
            language_code="ru",
        )
        attempt = await hub.portal_auth.create_login_attempt()
        assert hub.portal_auth.login_attempt_status(attempt) == "pending"

        approved = await hub.portal_auth.approve_login_attempt(attempt.token, user.id)
        assert approved.approved_user_id == user.id
        assert hub.portal_auth.login_attempt_status(approved) == "approved"

        consumed_user = await hub.portal_auth.consume_login_attempt(attempt.token)
        consumed_attempt = await hub.portal_auth.get_login_attempt(attempt.token)

    assert consumed_user.id == user.id
    assert consumed_attempt is not None
    assert consumed_attempt.consumed_at is not None


@pytest.mark.asyncio
async def test_portal_auth_attempt_can_be_canceled(test_services):
    async with test_services.hub() as hub:
        attempt = await hub.portal_auth.create_login_attempt()
        canceled = await hub.portal_auth.cancel_login_attempt(attempt.token)

    assert canceled.canceled_at is not None
