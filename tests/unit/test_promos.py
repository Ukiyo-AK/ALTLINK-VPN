from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from altlink.application.services.base import ConflictError
from altlink.domain.enums import BalanceTransactionType, PromoRewardKind, SubscriptionStatus, UserStatus
from altlink.infrastructure.db.models import BalanceTransaction, PromoCodeRedemption
from altlink.utils.time import ensure_utc, utc_now


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
        assert len(owner_promo.code) == 8
        assert owner_promo.code.isalnum()
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


@pytest.mark.asyncio
async def test_list_codes_hides_personal_codes_by_default(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=19504,
            username="hidden_personal_promo",
            first_name="Hidden",
            last_name="Promo",
            language_code="ru",
        )
        personal = await hub.promos.get_or_create_personal_discount_code(user.id)
        manual = await hub.promos.create_code(
            code="VISIBLE100",
            name="Visible manual promo",
            reward_kind=personal.reward_kind,
            reward_value=Decimal("10"),
            usage_limit=10,
            expires_at=None,
            new_users_only=False,
            admin_id=None,
        )

        default_codes = await hub.promos.list_codes()
        all_codes = await hub.promos.list_codes(include_personal=True)

    assert manual.id in {item.id for item in default_codes}
    assert personal.id not in {item.id for item in default_codes}
    assert personal.id in {item.id for item in all_codes}


@pytest.mark.asyncio
async def test_discount_promo_application_creates_visible_zero_value_transaction(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=19505,
            username="promo_transaction",
            first_name="Promo",
            last_name="Transaction",
            language_code="ru",
        )
        promo = await hub.promos.get_or_create_personal_discount_code(user.id)
        await hub.promos.redeem_code(user.id, promo.code)
        transaction = await hub.session.scalar(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == user.id,
                BalanceTransaction.type == BalanceTransactionType.PROMO_APPLIED,
            )
        )
        trial_is_still_available = await hub.accounts.can_offer_trial(user.id)

    assert transaction is not None
    assert transaction.amount_rub == Decimal("0.00")
    assert promo.code in transaction.description
    assert trial_is_still_available is True


@pytest.mark.asyncio
async def test_repeat_trial_promo_reactivates_consumed_trial_for_configured_days(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=19506,
            username="repeat_trial_promo",
            first_name="Repeat",
            last_name="Trial",
            language_code="ru",
        )
        previous = await hub.billing.activate_trial(user.id)
        previous.status = SubscriptionStatus.CANCELED
        previous.canceled_at = utc_now()
        user.status = UserStatus.CANCELED

        with pytest.raises(ConflictError, match="уже был использован"):
            await hub.billing.activate_trial(user.id)

        promo = await hub.promos.create_code(
            code="RETURN3",
            name="Repeat trial for three days",
            reward_kind=PromoRewardKind.REPEAT_TRIAL,
            reward_value=Decimal("3"),
            usage_limit=10,
            expires_at=None,
            new_users_only=False,
            admin_id=None,
        )
        activated_at = utc_now()
        redeemed_promo, redemption, message = await hub.billing.redeem_promo_code(user.id, promo.code)
        current = await hub.accounts.get_current_subscription(user.id)
        refreshed_redemption = await hub.session.get(PromoCodeRedemption, redemption.id)

    assert redeemed_promo.id == promo.id
    assert current is not None
    assert current.status == SubscriptionStatus.TRIAL
    current_ends_at = ensure_utc(current.ends_at)
    assert activated_at + timedelta(days=3) - timedelta(seconds=5) <= current_ends_at
    assert current_ends_at <= activated_at + timedelta(days=3) + timedelta(seconds=5)
    assert refreshed_redemption.applied_at is not None
    assert refreshed_redemption.applied_subscription_id == current.id
    assert refreshed_redemption.reward_value_applied == Decimal("3.00")
    assert promo.used_count == 1
    assert "Повторный тест на 3 дн. активирован" in message


@pytest.mark.asyncio
async def test_repeat_trial_promo_is_not_consumed_while_access_is_active(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=19507,
            username="active_repeat_trial_promo",
            first_name="Active",
            last_name="Trial",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)
        promo = await hub.promos.create_code(
            code="LATER2",
            name="Repeat trial later",
            reward_kind=PromoRewardKind.REPEAT_TRIAL,
            reward_value=Decimal("2"),
            usage_limit=1,
            expires_at=None,
            new_users_only=False,
            admin_id=None,
        )

        with pytest.raises(ConflictError, match="после окончания текущего доступа"):
            await hub.billing.redeem_promo_code(user.id, promo.code)

        redemption = await hub.session.scalar(
            select(PromoCodeRedemption).where(PromoCodeRedemption.promo_code_id == promo.id)
        )

    assert redemption is None
    assert promo.used_count == 0


@pytest.mark.asyncio
async def test_broadcast_promo_list_is_paginated_and_excludes_unavailable_codes(test_services):
    async with test_services.hub() as hub:
        valid_codes = []
        for index in range(3):
            valid_codes.append(
                await hub.promos.create_code(
                    code=f"MAIL{index}",
                    name=f"Broadcast promo {index}",
                    reward_kind=PromoRewardKind.PLAN_DISCOUNT,
                    reward_value=Decimal("10"),
                    usage_limit=10,
                    expires_at=None,
                    new_users_only=False,
                    admin_id=None,
                )
            )
        inactive = await hub.promos.create_code(
            code="INACTIVE",
            name="Inactive broadcast promo",
            reward_kind=PromoRewardKind.BALANCE,
            reward_value=Decimal("10"),
            usage_limit=10,
            expires_at=None,
            new_users_only=False,
            admin_id=None,
        )
        inactive.is_active = False
        expired = await hub.promos.create_code(
            code="EXPIRED",
            name="Expired broadcast promo",
            reward_kind=PromoRewardKind.BALANCE,
            reward_value=Decimal("10"),
            usage_limit=10,
            expires_at=utc_now() - timedelta(minutes=1),
            new_users_only=False,
            admin_id=None,
        )
        exhausted = await hub.promos.create_code(
            code="EXHAUSTED",
            name="Exhausted broadcast promo",
            reward_kind=PromoRewardKind.BALANCE,
            reward_value=Decimal("10"),
            usage_limit=1,
            expires_at=None,
            new_users_only=False,
            admin_id=None,
        )
        exhausted.used_count = 1
        await hub.session.flush()

        first_page, total = await hub.promos.list_broadcast_codes(page=0, page_size=2)
        second_page, second_total = await hub.promos.list_broadcast_codes(page=1, page_size=2)

    visible_ids = {item.id for item in first_page + second_page}
    assert total == second_total == 3
    assert len(first_page) == 2
    assert len(second_page) == 1
    assert visible_ids == {item.id for item in valid_codes}
    assert inactive.id not in visible_ids
    assert expired.id not in visible_ids
    assert exhausted.id not in visible_ids
