from __future__ import annotations

from decimal import Decimal

import pytest

from altlink.application.services.base import ConflictError
from altlink.utils.time import utc_now


@pytest.mark.asyncio
async def test_personal_discount_code_is_stable_unique_and_bound_to_owner(test_services):
    async with test_services.hub() as hub:
        owner = await hub.accounts.get_or_create_user(
            telegram_id=19501,
            username="personal_promo_owner",
            first_name="Promo",
            last_name="Owner",
            language_code="ru",
        )
        other = await hub.accounts.get_or_create_user(
            telegram_id=19502,
            username="personal_promo_other",
            first_name="Promo",
            last_name="Other",
            language_code="ru",
        )

        owner_promo = await hub.promos.get_or_create_personal_discount_code(owner.id)
        owner_promo_again = await hub.promos.get_or_create_personal_discount_code(owner.id)
        other_promo = await hub.promos.get_or_create_personal_discount_code(other.id)

        assert owner_promo.id == owner_promo_again.id
        assert owner_promo.code.startswith("ALT10-")
        assert owner_promo.code != other_promo.code
        assert owner_promo.assigned_user_id == owner.id
        assert owner_promo.usage_limit == 1
        assert owner_promo.reward_value == Decimal("10.00")

        with pytest.raises(ConflictError, match="другого пользователя"):
            await hub.promos.redeem_code(other.id, owner_promo.code)

        promo, redemption, result = await hub.promos.redeem_code(owner.id, owner_promo.code)
        repeated_promo, repeated_redemption, repeated_result = await hub.promos.redeem_code(
            owner.id,
            owner_promo.code,
        )

    assert promo.id == repeated_promo.id
    assert redemption.id == repeated_redemption.id
    assert "автоматическое продление" in result
    assert "уже активирован" in repeated_result


@pytest.mark.asyncio
async def test_next_campaign_creates_new_code_only_after_previous_discount_was_consumed(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=19503,
            username="repeat_personal_promo",
            first_name="Repeat",
            last_name="Promo",
            language_code="ru",
        )
        first = await hub.promos.get_or_create_personal_discount_code(
            user.id,
            campaign_key="inactive:2026-06",
        )
        _, redemption, _ = await hub.promos.redeem_code(user.id, first.code)

        pending_next_campaign = await hub.promos.get_or_create_personal_discount_code(
            user.id,
            campaign_key="inactive:2026-07",
        )
        assert pending_next_campaign.id == first.id

        redemption.applied_at = utc_now()
        redemption.reward_value_applied = Decimal("10.00")
        consumed_next_campaign = await hub.promos.get_or_create_personal_discount_code(
            user.id,
            campaign_key="inactive:2026-07",
        )

    assert consumed_next_campaign.id != first.id
    assert consumed_next_campaign.code != first.code
    assert consumed_next_campaign.assigned_user_id == user.id
