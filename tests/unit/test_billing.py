from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from altlink.application.services.billing import BillingService
from altlink.application.services.base import ConflictError
from altlink.domain.billing import compute_period_end, compute_prorated_daily_charge, quantize_money
from altlink.domain.enums import (
    BalanceTransactionType,
    NotificationStatus,
    NotificationType,
    PlanCode,
    PromoRewardKind,
    ServerType,
    SubscriptionStatus,
    TrafficLimitStrategy,
    UserStatus,
)
from altlink.domain.plans import (
    SINGLE_10GBIT_MONTHLY_PRICE_RUB,
    WHITELIST_BILLING_VERSION,
    whitelist_included_bytes,
)
from altlink.infrastructure.db.models import BalanceTransaction, PromoCodeRedemption, Subscription, TrialPeriod
from altlink.infrastructure.remnawave_schemas import RemoteSeriesPoint, RemoteUsageResponse, RemoteUsageTopNode
from altlink.utils.time import ensure_utc, utc_now


def test_compute_period_end_uses_fixed_day_window():
    started_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert compute_period_end(started_at, 30) == datetime(2026, 1, 31, 12, 0, tzinfo=UTC)


def test_prorated_daily_charges_sum_to_monthly_price():
    charges = [compute_prorated_daily_charge(Decimal("200"), 30, day) for day in range(1, 31)]
    assert quantize_money(sum(charges, Decimal("0"))) == Decimal("200.00")
    assert charges[0] == Decimal("6.67")


def test_low_balance_reminder_window_matches_expected_checkpoints():
    assert BillingService._low_balance_reminder_window(timedelta(days=2)) == ("3d", "меньше 3 дней")
    assert BillingService._low_balance_reminder_window(timedelta(hours=12)) == ("1d", "меньше 1 дня")
    assert BillingService._low_balance_reminder_window(timedelta(minutes=30)) == ("1h", "меньше 1 часа")
    assert BillingService._low_balance_reminder_window(timedelta(days=5)) is None
    assert BillingService._low_balance_reminder_window(timedelta(0)) is None


def test_trial_reminder_window_matches_expected_checkpoints():
    assert BillingService._trial_reminder_window(timedelta(hours=20)) == ("24h", "24 часа")
    assert BillingService._trial_reminder_window(timedelta(hours=2)) == ("3h", "3 часа")
    assert BillingService._trial_reminder_window(timedelta(minutes=30)) == ("1h", "1 час")
    assert BillingService._trial_reminder_window(timedelta(days=2)) is None
    assert BillingService._trial_reminder_window(timedelta(0)) is None


@pytest.mark.asyncio
async def test_paid_plan_activation_quote_includes_promo_balance_and_carryover(
    test_services,
    monkeypatch,
):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=12998,
            username="activation_quote",
            first_name="Activation",
            last_name="Quote",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("69"), auto_complete=True)
        await hub.billing.activate_paid_plan(
            user.id,
            PlanCode.SINGLE_10GBIT,
            charge_user=True,
        )
        user.balance_rub = Decimal("31.00")
        promo = await hub.promos.get_or_create_personal_discount_code(user.id)
        _, redemption, _ = await hub.promos.redeem_code(user.id, promo.code)
        monkeypatch.setattr(
            hub.billing,
            "_calculate_residual_credit_rub",
            lambda _subscription: Decimal("34.50"),
        )

        quote = await hub.billing.quote_paid_plan_activation(
            user.id,
            PlanCode.UNLIMITED,
        )

        assert quote.discount_rub == Decimal("19.90")
        assert quote.discounted_price_rub == Decimal("179.10")
        assert quote.carryover_credit_rub == Decimal("34.50")
        assert quote.effective_available_balance_rub == Decimal("65.50")
        assert quote.required_topup_rub == Decimal("113.60")
        assert Decimal(user.balance_rub) == Decimal("31.00")
        assert redemption.applied_at is None


@pytest.mark.asyncio
async def test_personal_traffic_limit_survives_plan_change_and_catalog_sync(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=12999,
            username="limited_account",
            first_name="Limited",
            last_name="Account",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("1000"), auto_complete=True)
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)

        await hub.billing.set_user_traffic_limit(
            user.id,
            limit_gb=Decimal("123.5"),
            strategy=TrafficLimitStrategy.MONTH,
        )
        remote = test_services.remnawave.users[user.remnawave_user_uuid]
        assert remote.trafficLimitBytes == int(Decimal("123.5") * 1024**3)
        assert remote.trafficLimitStrategy == "MONTH"

        await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED, charge_user=True)
        await hub.catalog.sync_user_target_squads(user.id)
        refreshed = await hub.accounts.get_user(user.id)
        remote = test_services.remnawave.users[refreshed.remnawave_user_uuid]

    assert refreshed.traffic_limit_bytes_override == int(Decimal("123.5") * 1024**3)
    assert refreshed.traffic_limit_strategy_override == TrafficLimitStrategy.MONTH
    assert remote.trafficLimitBytes == int(Decimal("123.5") * 1024**3)
    assert remote.trafficLimitStrategy == "MONTH"


@pytest.mark.asyncio
async def test_process_due_subscriptions_queues_trial_expiring_reminder(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13000,
            username="trial_reminder",
            first_name="Trial",
            last_name="Reminder",
            language_code="ru",
        )
        subscription = await hub.billing.activate_trial(user.id)
        subscription.ends_at = utc_now() + timedelta(hours=20)
        subscription.next_billing_at = subscription.ends_at

        await hub.billing.process_due_subscriptions()

        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    reminder = next(item for item in pending if item.user_id == user.id and item.type == NotificationType.BROADCAST)
    assert reminder.dedupe_key == f"trial-reminder:{subscription.id}:24h"
    assert "24 часа" in reminder.message
    assert "Пробный период" in reminder.message


@pytest.mark.asyncio
async def test_process_due_subscriptions_commits_in_small_checkpoints(test_services, monkeypatch):
    async with test_services.hub() as hub:
        user_one = await hub.accounts.get_or_create_user(
            telegram_id=13006,
            username="trial_checkpoint_one",
            first_name="Trial",
            last_name="CheckpointOne",
            language_code="ru",
        )
        subscription_one = await hub.billing.activate_trial(user_one.id)
        subscription_one.ends_at = utc_now() + timedelta(hours=20)
        subscription_one.next_billing_at = subscription_one.ends_at

        user_two = await hub.accounts.get_or_create_user(
            telegram_id=13007,
            username="trial_checkpoint_two",
            first_name="Trial",
            last_name="CheckpointTwo",
            language_code="ru",
        )
        subscription_two = await hub.billing.activate_trial(user_two.id)
        subscription_two.ends_at = utc_now() + timedelta(hours=20)
        subscription_two.next_billing_at = subscription_two.ends_at

        original_commit = hub.session.commit
        commit_calls = 0

        async def counting_commit():
            nonlocal commit_calls
            commit_calls += 1
            return await original_commit()

        monkeypatch.setattr(hub.session, "commit", counting_commit)

        await hub.billing.process_due_subscriptions()

    assert commit_calls >= 2


@pytest.mark.asyncio
async def test_sync_user_trial_state_expires_overdue_trial_and_queues_notifications_even_if_remote_disable_fails(
    test_services,
    monkeypatch,
):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13004,
            username="trial_expired_sync",
            first_name="Trial",
            last_name="Expired",
            language_code="ru",
        )
        subscription = await hub.billing.activate_trial(user.id)
        subscription.ends_at = utc_now() - timedelta(minutes=5)
        subscription.next_billing_at = subscription.ends_at

        async def failing_disable_user(uuid: str):
            raise httpx.ConnectError("panel offline")

        monkeypatch.setattr(test_services.remnawave, "disable_user", failing_disable_user)

        current = await hub.billing.sync_user_trial_state(user.id)
        latest = await hub.accounts.get_latest_subscription(user.id)
        refreshed_user = await hub.accounts.get_user(user.id)
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    assert current is None
    assert latest is not None
    assert latest.status == SubscriptionStatus.EXPIRED
    assert refreshed_user.status == UserStatus.BLOCKED
    assert any(item.user_id == user.id and item.type == NotificationType.TRIAL_ENDED for item in pending)


@pytest.mark.asyncio
async def test_process_due_subscriptions_queues_trial_followup_for_expired_trial_without_paid_history(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13005,
            username="trial_followup",
            first_name="Trial",
            last_name="Followup",
            language_code="ru",
        )
        registered_at = utc_now() - timedelta(days=4)
        user.registration_completed_at = registered_at
        user.consent_accepted_at = registered_at
        subscription = await hub.billing.activate_trial(user.id)
        subscription.status = SubscriptionStatus.EXPIRED
        subscription.ends_at = utc_now() - timedelta(hours=13)
        subscription.next_billing_at = subscription.ends_at
        user.status = UserStatus.BLOCKED

        await hub.billing.process_due_subscriptions()
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    followup = next(item for item in pending if item.user_id == user.id and item.dedupe_key == f"trial-followup:{subscription.id}:12h")
    assert followup.type == NotificationType.BROADCAST
    assert len(followup.payload["promo_code"]) == 8
    assert f"<code>{followup.payload['promo_code']}</code>" in followup.message
    assert not any(
        item.user_id == user.id and item.dedupe_key == f"inactive-promo:{user.id}:{utc_now().strftime('%Y-%m')}"
        for item in pending
    )


@pytest.mark.asyncio
async def test_process_due_subscriptions_queues_setup_help_for_inactive_trial(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13006,
            username="trial_no_connection",
            first_name="Trial",
            last_name="NoConnection",
            language_code="ru",
        )
        subscription = await hub.billing.activate_trial(user.id)
        started_at = utc_now() - timedelta(hours=4)
        subscription.started_at = started_at
        subscription.ends_at = started_at + timedelta(days=2)
        subscription.next_billing_at = subscription.ends_at
        subscription.billing_anchor_at = started_at

        await hub.billing.process_due_subscriptions()
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    notification = next(
        item
        for item in pending
        if item.user_id == user.id and item.dedupe_key == f"trial-setup-help:{subscription.id}:3h"
    )
    assert notification.type == NotificationType.BROADCAST
    assert "@altlink_support" in notification.message
    assert notification.payload["cta"] == "trial_setup_help"


@pytest.mark.asyncio
async def test_process_due_subscriptions_skips_setup_help_for_trial_with_traffic(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13007,
            username="trial_with_traffic",
            first_name="Trial",
            last_name="Connected",
            language_code="ru",
        )
        subscription = await hub.billing.activate_trial(user.id)
        started_at = utc_now() - timedelta(hours=4)
        subscription.started_at = started_at
        subscription.ends_at = started_at + timedelta(days=2)
        subscription.next_billing_at = subscription.ends_at
        subscription.billing_anchor_at = started_at
        subscription.traffic_used_bytes = 1024

        await hub.billing.process_due_subscriptions()
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    assert not any(item.user_id == user.id and item.dedupe_key == f"trial-setup-help:{subscription.id}:3h" for item in pending)


@pytest.mark.asyncio
async def test_process_due_subscriptions_queues_monthly_promo_for_registered_users_without_paid_history(
    test_services,
):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13002,
            username="promo_waiting",
            first_name="Promo",
            last_name="Waiting",
            language_code="ru",
        )
        registered_at = utc_now() - timedelta(days=4)
        user.registration_completed_at = registered_at
        user.consent_accepted_at = registered_at

        await hub.billing.process_due_subscriptions()
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

        await hub.billing.process_due_subscriptions()
        pending_again = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    promo = next(item for item in pending if item.user_id == user.id and item.type == NotificationType.PROMO_CODE)
    assert promo.dedupe_key == f"inactive-promo:{user.id}:{utc_now().strftime('%Y-%m')}"
    assert len(promo.payload["promo_code"]) == 8
    assert promo.payload["promo_code"] != "ALT10"
    assert f"<code>{promo.payload['promo_code']}</code>" in promo.message
    assert "10%" in promo.message
    assert len([item for item in pending_again if item.user_id == user.id and item.type == NotificationType.PROMO_CODE]) == 1


@pytest.mark.asyncio
async def test_first_inactive_promo_waits_three_full_days_after_registration(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13036,
            username="promo_three_day_delay",
            first_name="Promo",
            last_name="Delay",
            language_code="ru",
        )
        not_ready_at = utc_now() - timedelta(days=2, hours=23)
        user.registration_completed_at = not_ready_at
        user.consent_accepted_at = not_ready_at

        await hub.billing.process_due_subscriptions()
        pending_before = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

        ready_at = utc_now() - timedelta(days=3, minutes=1)
        user.registration_completed_at = ready_at
        user.consent_accepted_at = ready_at
        await hub.billing.process_due_subscriptions()
        pending_after = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    assert not any(
        item.user_id == user.id and item.type == NotificationType.PROMO_CODE
        for item in pending_before
    )
    assert any(
        item.user_id == user.id and item.type == NotificationType.PROMO_CODE
        for item in pending_after
    )


@pytest.mark.asyncio
async def test_long_absent_trial_user_receives_return_trial_offer(test_services, monkeypatch):
    monkeypatch.setattr(BillingService, "_choose_return_trial", staticmethod(lambda *_: True))
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13037,
            username="return_trial_offer",
            first_name="Return",
            last_name="Trial",
            language_code="ru",
        )
        registered_at = utc_now() - timedelta(days=45)
        user.registration_completed_at = registered_at
        user.consent_accepted_at = registered_at
        subscription = await hub.billing.activate_trial(user.id)
        ended_at = utc_now() - timedelta(days=31)
        subscription.status = SubscriptionStatus.EXPIRED
        subscription.ends_at = ended_at
        subscription.next_billing_at = ended_at
        user.status = UserStatus.BLOCKED
        trial_period = await hub.accounts.session.scalar(
            select(TrialPeriod).where(TrialPeriod.user_id == user.id)
        )
        trial_period.ends_at = ended_at

        await hub.billing.process_due_subscriptions()
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    notification = next(
        item
        for item in pending
        if item.user_id == user.id and item.dedupe_key == f"return-trial:{user.id}:{utc_now().strftime('%Y-%m')}"
    )
    assert notification.type == NotificationType.BROADCAST
    assert notification.payload["cta"] == "return_trial"
    assert "бесплат" in notification.message.casefold()
    assert not any(
        item.user_id == user.id and item.payload and item.payload.get("cta") == "trial_followup"
        for item in pending
    )


@pytest.mark.asyncio
async def test_long_absent_trial_user_can_receive_deep_discount(test_services, monkeypatch):
    monkeypatch.setattr(BillingService, "_choose_return_trial", staticmethod(lambda *_: False))
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13038,
            username="return_trial_discount",
            first_name="Return",
            last_name="Discount",
            language_code="ru",
        )
        registered_at = utc_now() - timedelta(days=45)
        user.registration_completed_at = registered_at
        user.consent_accepted_at = registered_at
        subscription = await hub.billing.activate_trial(user.id)
        ended_at = utc_now() - timedelta(days=31)
        subscription.status = SubscriptionStatus.EXPIRED
        subscription.ends_at = ended_at
        subscription.next_billing_at = ended_at
        user.status = UserStatus.BLOCKED
        trial_period = await hub.session.scalar(select(TrialPeriod).where(TrialPeriod.user_id == user.id))
        trial_period.ends_at = ended_at

        await hub.billing.process_due_subscriptions()
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    notification = next(
        item
        for item in pending
        if item.user_id == user.id and item.type == NotificationType.PROMO_CODE
    )
    assert notification.payload["discount_percent"] == 35
    assert notification.dedupe_key == f"inactive-promo-trial-deep:{user.id}:{utc_now().strftime('%Y-%m')}"
    assert "35%" in notification.message


@pytest.mark.asyncio
async def test_personal_campaign_codes_are_unique_for_different_users(test_services):
    async with test_services.hub() as hub:
        users = []
        for telegram_id in (13030, 13031):
            user = await hub.accounts.get_or_create_user(
                telegram_id=telegram_id,
                username=f"promo_{telegram_id}",
                first_name="Promo",
                last_name="Unique",
                language_code="ru",
            )
            registered_at = utc_now() - timedelta(days=4)
            user.registration_completed_at = registered_at
            user.consent_accepted_at = registered_at
            users.append(user)

        await hub.billing.process_due_subscriptions()
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    promo_codes = {
        item.user_id: item.payload["promo_code"]
        for item in pending
        if item.type == NotificationType.PROMO_CODE
    }
    assert promo_codes[users[0].id] != promo_codes[users[1].id]


@pytest.mark.asyncio
async def test_pending_legacy_alt10_notification_is_upgraded_to_personal_code(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13034,
            username="legacy_alt10_pending",
            first_name="Legacy",
            last_name="Promo",
            language_code="ru",
        )
        registered_at = utc_now() - timedelta(days=4)
        user.registration_completed_at = registered_at
        user.consent_accepted_at = registered_at
        dedupe_key = f"inactive-promo:{user.id}:{utc_now().strftime('%Y-%m')}"
        legacy = await hub.notifications.queue(
            user_id=user.id,
            notification_type=NotificationType.PROMO_CODE,
            message="Промокод: <code>ALT10</code>",
            payload={
                "promo_code": "ALT10",
                "cta": "inactive_promo",
                "parse_mode": "HTML",
            },
            dedupe_key=dedupe_key,
        )

        await hub.billing.process_due_subscriptions()
        refreshed = await hub.session.get(type(legacy), legacy.id)

    assert refreshed is not None
    assert len(refreshed.payload["promo_code"]) == 8
    assert refreshed.payload["promo_code"] != "ALT10"
    assert f"<code>{refreshed.payload['promo_code']}</code>" in refreshed.message


@pytest.mark.asyncio
async def test_process_due_subscriptions_skips_promo_for_users_with_active_paid_subscription(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13003,
            username="promo_paid",
            first_name="Promo",
            last_name="Paid",
            language_code="ru",
        )
        registered_at = utc_now() - timedelta(days=4)
        user.registration_completed_at = registered_at
        user.consent_accepted_at = registered_at
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)

        await hub.billing.process_due_subscriptions()
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    assert not any(item.user_id == user.id and item.type == NotificationType.PROMO_CODE for item in pending)


@pytest.mark.asyncio
async def test_recent_lapsed_paid_user_receives_ten_percent_discount_after_one_day(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13035,
            username="promo_lapsed_paid",
            first_name="Promo",
            last_name="Lapsed",
            language_code="ru",
        )
        registered_at = utc_now() - timedelta(days=60)
        user.registration_completed_at = registered_at
        user.consent_accepted_at = registered_at

        old_promo = await hub.promos.get_or_create_personal_discount_code(
            user.id,
            campaign_key="inactive:previous",
        )
        previous_campaign = await hub.notifications.queue(
            user_id=user.id,
            notification_type=NotificationType.PROMO_CODE,
            message="Предыдущая скидка",
            payload={"promo_code": old_promo.code},
            dedupe_key=f"inactive-promo:{user.id}:{(utc_now() - timedelta(days=40)).strftime('%Y-%m')}",
        )
        previous_campaign.status = NotificationStatus.SENT
        await hub.promos.redeem_code(user.id, old_promo.code)
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(
            user.id,
            PlanCode.SINGLE_10GBIT,
            charge_user=True,
        )
        subscription.status = SubscriptionStatus.BLOCKED
        inactive_at = utc_now() - timedelta(days=2)
        subscription.blocked_at = inactive_at
        subscription.ends_at = inactive_at
        subscription.next_billing_at = inactive_at
        user.status = UserStatus.BLOCKED

        await hub.billing.process_due_subscriptions()
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    notification = next(
        item
        for item in pending
        if item.user_id == user.id and item.type == NotificationType.PROMO_CODE
    )
    assert len(notification.payload["promo_code"]) == 8
    assert notification.payload["promo_code"] != old_promo.code
    assert notification.payload["discount_percent"] == 10
    assert notification.dedupe_key == (
        f"inactive-promo-lapsed-fresh:{user.id}:{utc_now().strftime('%Y-%m')}"
    )
    assert "10%" in notification.message


@pytest.mark.asyncio
async def test_current_month_generic_promo_blocks_new_lapsed_campaign(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13042,
            username="generic_promo_block",
            first_name="Generic",
            last_name="PromoBlock",
            language_code="ru",
        )
        registered_at = utc_now() - timedelta(days=60)
        user.registration_completed_at = registered_at
        user.consent_accepted_at = registered_at
        previous_campaign = await hub.notifications.queue(
            user_id=user.id,
            notification_type=NotificationType.PROMO_CODE,
            message="Старая промо-рассылка",
            payload={"cta": "inactive_promo", "promo_code": "OLDPROMO"},
            dedupe_key=f"inactive-promo:{user.id}:{utc_now().strftime('%Y-%m')}",
        )
        previous_campaign.status = NotificationStatus.SENT

        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(
            user.id,
            PlanCode.SINGLE_10GBIT,
            charge_user=True,
        )
        inactive_at = utc_now() - timedelta(days=2)
        subscription.status = SubscriptionStatus.BLOCKED
        subscription.blocked_at = inactive_at
        subscription.ends_at = inactive_at
        subscription.next_billing_at = inactive_at
        user.status = UserStatus.BLOCKED

        await hub.billing.process_due_subscriptions()
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    assert not any(
        item.user_id == user.id
        and item.dedupe_key == f"inactive-promo-lapsed-fresh:{user.id}:{utc_now().strftime('%Y-%m')}"
        for item in pending
    )


@pytest.mark.asyncio
async def test_legacy_lapsed_promo_key_blocks_new_lapsed_campaign(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13040,
            username="legacy_lapsed_block",
            first_name="Legacy",
            last_name="Lapsed",
            language_code="ru",
        )
        registered_at = utc_now() - timedelta(days=60)
        user.registration_completed_at = registered_at
        user.consent_accepted_at = registered_at

        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(
            user.id,
            PlanCode.SINGLE_10GBIT,
            charge_user=True,
        )
        inactive_at = utc_now() - timedelta(days=2)
        subscription.status = SubscriptionStatus.BLOCKED
        subscription.blocked_at = inactive_at
        subscription.ends_at = inactive_at
        subscription.next_billing_at = inactive_at
        user.status = UserStatus.BLOCKED
        legacy = await hub.notifications.queue(
            user_id=user.id,
            notification_type=NotificationType.PROMO_CODE,
            message="Старая промо-рассылка",
            payload={"cta": "inactive_promo", "promo_code": "OLDPROMO"},
            dedupe_key=f"inactive-promo-lapsed:{user.id}:{utc_now().strftime('%Y-%m')}",
        )
        legacy.status = NotificationStatus.SENT

        await hub.billing.process_due_subscriptions()
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    assert not any(
        item.user_id == user.id
        and item.dedupe_key == f"inactive-promo-lapsed-fresh:{user.id}:{utc_now().strftime('%Y-%m')}"
        for item in pending
    )


@pytest.mark.asyncio
async def test_deep_lapsed_paid_user_can_receive_thirty_five_percent_discount(test_services, monkeypatch):
    monkeypatch.setattr(BillingService, "_choose_return_trial", staticmethod(lambda *_: False))
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13039,
            username="promo_deep_lapsed_paid",
            first_name="Promo",
            last_name="DeepLapsed",
            language_code="ru",
        )
        registered_at = utc_now() - timedelta(days=70)
        user.registration_completed_at = registered_at
        user.consent_accepted_at = registered_at

        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(
            user.id,
            PlanCode.SINGLE_10GBIT,
            charge_user=True,
        )
        inactive_at = utc_now() - timedelta(days=31)
        subscription.status = SubscriptionStatus.BLOCKED
        subscription.blocked_at = inactive_at
        subscription.ends_at = inactive_at
        subscription.next_billing_at = inactive_at
        user.status = UserStatus.BLOCKED

        await hub.billing.process_due_subscriptions()
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    notification = next(
        item
        for item in pending
        if item.user_id == user.id and item.type == NotificationType.PROMO_CODE
    )
    assert notification.payload["discount_percent"] == 35
    assert notification.dedupe_key == (
        f"inactive-promo-lapsed-deep:{user.id}:{utc_now().strftime('%Y-%m')}"
    )
    assert "35%" in notification.message


@pytest.mark.asyncio
async def test_existing_monthly_trial_followup_blocks_deep_trial_campaign(test_services, monkeypatch):
    monkeypatch.setattr(BillingService, "_choose_return_trial", staticmethod(lambda *_: False))
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13041,
            username="legacy_trial_followup_block",
            first_name="Legacy",
            last_name="TrialFollowup",
            language_code="ru",
        )
        registered_at = utc_now() - timedelta(days=45)
        user.registration_completed_at = registered_at
        user.consent_accepted_at = registered_at
        subscription = await hub.billing.activate_trial(user.id)
        ended_at = utc_now() - timedelta(days=31)
        subscription.status = SubscriptionStatus.EXPIRED
        subscription.ends_at = ended_at
        subscription.next_billing_at = ended_at
        user.status = UserStatus.BLOCKED
        trial_period = await hub.session.scalar(select(TrialPeriod).where(TrialPeriod.user_id == user.id))
        trial_period.ends_at = ended_at
        legacy = await hub.notifications.queue(
            user_id=user.id,
            notification_type=NotificationType.BROADCAST,
            message="Старый follow-up после теста",
            payload={"cta": "trial_followup", "promo_code": "OLDPROMO"},
            dedupe_key=f"trial-followup:{subscription.id}:12h",
        )
        legacy.status = NotificationStatus.SENT

        await hub.billing.process_due_subscriptions()
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    assert not any(
        item.user_id == user.id
        and item.dedupe_key == f"inactive-promo-trial-deep:{user.id}:{utc_now().strftime('%Y-%m')}"
        for item in pending
    )


@pytest.mark.asyncio
async def test_snapshot_traffic_uses_node_usage_for_whitelist_tracking_when_user_top_nodes_miss_it(
    test_services,
    monkeypatch,
):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13001,
            username="whitelist_metered",
            first_name="White",
            last_name="List",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        user = await hub.accounts.get_user(user.id)

        whitelist_node = next(node for node in test_services.remnawave.nodes.values() if "Whitelist" in node.name)
        test_services.remnawave.set_usage(
            user.remnawave_user_uuid,
            used_bytes=12 * 1024**3,
            lifetime_used_bytes=12 * 1024**3,
        )
        test_services.remnawave.set_node_usage(
            whitelist_node.uuid,
            user.remnawave_user_uuid,
            3 * 1024**3,
        )

        async def fake_get_user_usage(user_uuid, start, end):
            return RemoteUsageResponse(
                categories=[start.isoformat(), end.isoformat()],
                sparklineData=[0, 0],
                topNodes=[
                    RemoteUsageTopNode(
                        uuid="non-whitelist-node",
                        color="#00aaff",
                        name="Regular Node",
                        countryCode="DE",
                        total=9 * 1024**3,
                    )
                ],
                series=[
                    RemoteSeriesPoint(
                        uuid="non-whitelist-node",
                        name="Regular Node",
                        color="#00aaff",
                        countryCode="DE",
                        total=9 * 1024**3,
                        data=[9 * 1024**3, 0],
                    )
                ],
            )

        monkeypatch.setattr(test_services.remnawave, "get_user_usage", fake_get_user_usage)

        await hub.billing.snapshot_traffic()

        refreshed = await hub.accounts.get_current_subscription(user.id)

    assert refreshed is not None
    assert refreshed.traffic_used_bytes == 12 * 1024**3
    assert refreshed.whitelist_traffic_used_bytes == 3 * 1024**3


@pytest.mark.asyncio
async def test_start_whitelist_traffic_is_charged_immediately_and_caps_balance_at_configured_floor(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13021,
            username="start_instant_whitelist",
            first_name="Start",
            last_name="Instant",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("150"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        subscription.whitelist_billing_version = 1
        subscription.whitelist_included_limit_bytes = 0
        whitelist_node = next(node for node in test_services.remnawave.nodes.values() if "Whitelist" in node.name)

        test_services.remnawave.set_usage(
            user.remnawave_user_uuid,
            used_bytes=200 * 1024**3,
            lifetime_used_bytes=200 * 1024**3,
        )
        test_services.remnawave.set_node_usage(
            whitelist_node.uuid,
            user.remnawave_user_uuid,
            200 * 1024**3,
        )

        await hub.billing.snapshot_traffic()

        refreshed_user = await hub.accounts.get_user(user.id)
        refreshed_subscription = await hub.accounts.get_current_subscription(user.id)
        active_accesses = await hub.catalog.get_user_servers(user.id)
        servers = await hub.catalog.list_servers()
        remote_user = await test_services.remnawave.get_user(refreshed_user.remnawave_user_uuid)
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=20))).all())

    assert refreshed_subscription is not None
    assert Decimal(refreshed_user.balance_rub) == Decimal("-10.00")
    assert refreshed_subscription.whitelist_traffic_used_bytes == 200 * 1024**3
    assert refreshed_subscription.whitelist_traffic_billed_bytes == refreshed_subscription.whitelist_traffic_used_bytes
    active_server_types = {access.server.server_type for access in active_accesses if access.server}
    assert ServerType.TEN_GBIT in active_server_types
    assert ServerType.WHITELIST not in active_server_types
    whitelist_squad_ids = {
        server.remnawave_internal_squad_uuid
        for server in servers
        if server.server_type == ServerType.WHITELIST and server.remnawave_internal_squad_uuid
    }
    ten_gbit_squad_ids = {
        server.remnawave_internal_squad_uuid
        for server in servers
        if server.server_type == ServerType.TEN_GBIT and server.remnawave_internal_squad_uuid
    }
    remote_squad_ids = {item.uuid for item in remote_user.activeInternalSquads}
    assert remote_squad_ids.isdisjoint(whitelist_squad_ids)
    assert remote_squad_ids & ten_gbit_squad_ids
    notification = next(
        item
        for item in pending
        if item.user_id == user.id
        and item.type == NotificationType.BROADCAST
        and item.payload
        and item.payload.get("kind") == "start_whitelist_access_blocked"
    )
    assert "Доступ к серверам белых списков временно закрыт" in notification.message
    assert "Основной сервер Start продолжает работать" in notification.message
    assert notification.payload["cta"] == "access_blocked"


@pytest.mark.asyncio
async def test_start_whitelist_access_is_restored_after_balance_topup(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13031,
            username="start_whitelist_restore",
            first_name="Start",
            last_name="Restore",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        subscription.whitelist_billing_version = 1
        subscription.whitelist_included_limit_bytes = 0
        user.balance_rub = Decimal("-10.00")
        await hub.catalog.rebuild_user_access_matrix()

        blocked_accesses = await hub.catalog.get_user_servers(user.id)
        await hub.topups.create_request(user.id, Decimal("50"), auto_complete=True)
        restored_accesses = await hub.catalog.get_user_servers(user.id)
        remote_user = await test_services.remnawave.get_user(user.remnawave_user_uuid)

    blocked_types = {access.server.server_type for access in blocked_accesses if access.server}
    restored_types = {access.server.server_type for access in restored_accesses if access.server}
    restored_squad_ids = {
        access.server.remnawave_internal_squad_uuid
        for access in restored_accesses
        if access.server and access.server.remnawave_internal_squad_uuid
    }
    remote_squad_ids = {item.uuid for item in remote_user.activeInternalSquads}
    assert ServerType.TEN_GBIT in blocked_types
    assert ServerType.WHITELIST not in blocked_types
    assert ServerType.WHITELIST in restored_types
    assert remote_squad_ids == restored_squad_ids


@pytest.mark.asyncio
async def test_existing_start_user_at_whitelist_floor_gets_single_blocked_notice(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13043,
            username="start_whitelist_existing_floor",
            first_name="Start",
            last_name="ExistingFloor",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        subscription.whitelist_billing_version = 1
        subscription.whitelist_included_limit_bytes = 0
        whitelist_node = next(node for node in test_services.remnawave.nodes.values() if "Whitelist" in node.name)
        used_bytes = 2 * 1024**3

        user.balance_rub = Decimal("-10.00")
        subscription.whitelist_traffic_used_bytes = used_bytes
        subscription.whitelist_traffic_billed_bytes = used_bytes
        test_services.remnawave.set_usage(
            user.remnawave_user_uuid,
            used_bytes=used_bytes,
            lifetime_used_bytes=used_bytes,
        )
        test_services.remnawave.set_node_usage(
            whitelist_node.uuid,
            user.remnawave_user_uuid,
            used_bytes,
        )

        await hub.billing.snapshot_traffic()
        await hub.billing.snapshot_traffic()

        pending = [
            item
            for item in (await hub.session.scalars(await hub.notifications.pending_query(limit=30))).all()
            if item.user_id == user.id
            and item.type == NotificationType.BROADCAST
            and item.payload
            and item.payload.get("kind") == "start_whitelist_access_blocked"
        ]

    assert len(pending) == 1
    assert pending[0].payload["cta"] == "access_blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan_code", "included_gb"),
    [
        (PlanCode.SINGLE_10GBIT, 5),
        (PlanCode.SINGLE_10GBIT_WEEKLY, 1),
        (PlanCode.UNLIMITED, 50),
        (PlanCode.UNLIMITED_WEEKLY, 10),
    ],
)
async def test_new_paid_subscription_receives_versioned_whitelist_limit(test_services, plan_code, included_gb):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14000 + included_gb,
            username=f"wl_limit_{plan_code.value}",
            first_name="Limit",
            last_name="Test",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("1000"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, plan_code)

    assert subscription.whitelist_billing_version == WHITELIST_BILLING_VERSION
    assert subscription.whitelist_included_limit_bytes == included_gb * 1024**3
    assert subscription.whitelist_traffic_accounted_bytes == 0


@pytest.mark.asyncio
async def test_whitelist_packages_are_idempotent_and_sum(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14101,
            username="wl_packages",
            first_name="Package",
            last_name="Test",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        balance_before = Decimal(user.balance_rub)

        first = await hub.billing.purchase_whitelist_package(user.id, "25", request_key="package-once")
        repeated = await hub.billing.purchase_whitelist_package(user.id, "25", request_key="package-once")
        await hub.billing.purchase_whitelist_package(user.id, "50", request_key="package-twice")
        refreshed = await hub.accounts.get_user(user.id)

    assert first.id == repeated.id
    assert refreshed.whitelist_extra_traffic_bytes == 75 * 1024**3
    assert Decimal(refreshed.balance_rub) == balance_before - Decimal("130")


@pytest.mark.asyncio
async def test_whitelist_package_cannot_be_purchased_during_legacy_period(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14112,
            username="wl_legacy_package",
            first_name="Legacy",
            last_name="Package",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED)
        subscription.whitelist_billing_version = 1
        balance_before = Decimal(user.balance_rub)

        with pytest.raises(ConflictError, match="ближайшего продления"):
            await hub.billing.purchase_whitelist_package(user.id, "25", request_key="legacy-package")

    assert Decimal(user.balance_rub) == balance_before
    assert user.whitelist_extra_traffic_bytes == 0


@pytest.mark.asyncio
async def test_versioned_whitelist_uses_included_then_package_then_balance_without_debt(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14102,
            username="wl_order",
            first_name="Order",
            last_name="Test",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        user.whitelist_extra_traffic_bytes = 2 * 1024**3
        balance_before = Decimal(user.balance_rub)

        subscription.whitelist_traffic_used_bytes = 6 * 1024**3
        assert await hub.billing._apply_instant_whitelist_charges(user, subscription) is False
        assert user.whitelist_extra_traffic_bytes == 1 * 1024**3
        assert Decimal(user.balance_rub) == balance_before

        subscription.whitelist_traffic_used_bytes = 8 * 1024**3
        assert await hub.billing._apply_instant_whitelist_charges(user, subscription) is False
        assert user.whitelist_extra_traffic_bytes == 0
        assert Decimal(user.balance_rub) == balance_before - Decimal("2.00")

        user.balance_rub = Decimal("0.00")
        subscription.whitelist_traffic_used_bytes = 9 * 1024**3
        assert await hub.billing._apply_instant_whitelist_charges(user, subscription) is True
        assert Decimal(user.balance_rub) == Decimal("0.00")
        assert subscription.whitelist_traffic_accounted_bytes == 9 * 1024**3


@pytest.mark.asyncio
async def test_legacy_pro_keeps_old_whitelist_terms_until_renewal(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14103,
            username="legacy_pro",
            first_name="Legacy",
            last_name="Pro",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED)
        subscription.whitelist_billing_version = 1
        subscription.whitelist_included_limit_bytes = 0
        subscription.whitelist_traffic_used_bytes = 120 * 1024**3
        balance_before = Decimal(user.balance_rub)

        assert await hub.billing._apply_instant_whitelist_charges(user, subscription) is False
        assert Decimal(user.balance_rub) == balance_before

        user.whitelist_extra_traffic_bytes = 25 * 1024**3
        await hub.billing._start_next_billing_cycle(subscription, subscription.plan, renewed_at=utc_now())

    assert subscription.whitelist_billing_version == WHITELIST_BILLING_VERSION
    assert subscription.whitelist_included_limit_bytes == whitelist_included_bytes(PlanCode.UNLIMITED)
    assert subscription.whitelist_traffic_used_bytes == 0
    assert user.whitelist_extra_traffic_bytes == 25 * 1024**3


@pytest.mark.asyncio
async def test_versioned_whitelist_blocks_only_whitelist_at_exact_zero_limit(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14104,
            username="wl_exact_limit",
            first_name="Exact",
            last_name="Limit",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        user.balance_rub = Decimal("0.00")
        subscription.whitelist_traffic_used_bytes = subscription.whitelist_included_limit_bytes

        assert await hub.billing._apply_instant_whitelist_charges(user, subscription) is True
        await hub.catalog.rebuild_user_access_matrix()
        accesses = await hub.catalog.get_user_servers(user.id)

    server_types = {item.server.server_type for item in accesses if item.server is not None}
    assert ServerType.TEN_GBIT in server_types
    assert ServerType.WHITELIST not in server_types
    assert Decimal(user.balance_rub) == Decimal("0.00")


@pytest.mark.asyncio
async def test_versioned_whitelist_access_returns_after_balance_topup(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14111,
            username="wl_versioned_restore",
            first_name="Versioned",
            last_name="Restore",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        user.balance_rub = Decimal("0.00")
        subscription.whitelist_traffic_used_bytes = subscription.whitelist_included_limit_bytes
        await hub.billing._apply_instant_whitelist_charges(user, subscription)
        await hub.catalog.rebuild_user_access_matrix()
        blocked = await hub.catalog.get_user_servers(user.id)

        await hub.topups.create_request(user.id, Decimal("50"), auto_complete=True)
        restored = await hub.catalog.get_user_servers(user.id)

    blocked_types = {item.server.server_type for item in blocked if item.server is not None}
    restored_types = {item.server.server_type for item in restored if item.server is not None}
    assert ServerType.WHITELIST not in blocked_types
    assert ServerType.WHITELIST in restored_types


@pytest.mark.asyncio
async def test_versioned_whitelist_fractional_gigabyte_uses_decimal_money(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14105,
            username="wl_fraction",
            first_name="Fraction",
            last_name="Money",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        user.balance_rub = Decimal("10.00")
        subscription.whitelist_traffic_used_bytes = (
            subscription.whitelist_included_limit_bytes + 512 * 1024**2
        )

        await hub.billing._apply_instant_whitelist_charges(user, subscription)

    assert Decimal(user.balance_rub) == Decimal("9.00")
    assert subscription.whitelist_traffic_billed_bytes == 512 * 1024**2


@pytest.mark.asyncio
async def test_small_whitelist_increments_accumulate_before_money_rounding(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14110,
            username="wl_small_increments",
            first_name="Small",
            last_name="Increments",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        user.balance_rub = Decimal("1.00")
        subscription.whitelist_traffic_used_bytes = subscription.whitelist_included_limit_bytes + 1024**2

        await hub.billing._apply_instant_whitelist_charges(user, subscription)
        balance_after_first_increment = Decimal(user.balance_rub)
        subscription.whitelist_traffic_used_bytes = subscription.whitelist_included_limit_bytes + 3 * 1024**2
        await hub.billing._apply_instant_whitelist_charges(user, subscription)

    assert balance_after_first_increment == Decimal("1.00")
    assert Decimal(user.balance_rub) == Decimal("0.99")
    assert subscription.whitelist_traffic_billed_bytes == 3 * 1024**2


@pytest.mark.asyncio
async def test_versioned_whitelist_never_creates_debt_when_balance_covers_only_part(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14106,
            username="wl_no_debt",
            first_name="No",
            last_name="Debt",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        user.balance_rub = Decimal("0.50")
        subscription.whitelist_traffic_used_bytes = subscription.whitelist_included_limit_bytes + 1024**3

        assert await hub.billing._apply_instant_whitelist_charges(user, subscription) is True

    assert Decimal(user.balance_rub) >= Decimal("0.00")
    assert subscription.whitelist_traffic_accounted_bytes == subscription.whitelist_traffic_used_bytes
    assert subscription.whitelist_traffic_billed_bytes < 1024**3


@pytest.mark.asyncio
async def test_whitelist_limit_notifications_are_sent_once_per_threshold(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14107,
            username="wl_notifications",
            first_name="Limit",
            last_name="Notice",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        subscription.whitelist_traffic_used_bytes = 4 * 1024**3
        await hub.billing._apply_instant_whitelist_charges(user, subscription)
        await hub.billing._apply_instant_whitelist_charges(user, subscription)
        subscription.whitelist_traffic_used_bytes = 5 * 1024**3
        await hub.billing._apply_instant_whitelist_charges(user, subscription)
        await hub.billing._apply_instant_whitelist_charges(user, subscription)
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=100))).all())

    threshold_notifications = [
        item
        for item in pending
        if item.user_id == user.id
        and item.type == NotificationType.TRAFFIC_THRESHOLD
        and item.payload
        and item.payload.get("kind") == "whitelist_limit"
    ]
    assert [item.payload["threshold"] for item in threshold_notifications] == [80, 100]


@pytest.mark.asyncio
async def test_versioned_whitelist_blocked_notification_is_not_repeated_for_late_usage(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14113,
            username="wl_block_notice",
            first_name="Block",
            last_name="Notice",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        user.balance_rub = Decimal("0.00")
        subscription.whitelist_traffic_used_bytes = subscription.whitelist_included_limit_bytes + 1024**3
        await hub.billing._apply_instant_whitelist_charges(user, subscription)
        subscription.whitelist_traffic_used_bytes += 1024**3
        await hub.billing._apply_instant_whitelist_charges(user, subscription)
        pending = list((await hub.session.scalars(await hub.notifications.pending_query(limit=100))).all())

    blocked_notifications = [
        item
        for item in pending
        if item.user_id == user.id
        and item.payload
        and item.payload.get("kind") == "start_whitelist_access_blocked"
    ]
    assert len(blocked_notifications) == 1


@pytest.mark.asyncio
async def test_whitelist_usage_cursor_does_not_charge_pre_period_observation(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14108,
            username="wl_cursor",
            first_name="Cursor",
            last_name="Test",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        subscription.whitelist_usage_cursor_bytes = -1
        subscription.whitelist_traffic_used_bytes = 0

        hub.billing._record_whitelist_usage_observation(subscription, 100 * 1024**3)
        hub.billing._record_whitelist_usage_observation(subscription, 101 * 1024**3)

    assert subscription.whitelist_traffic_used_bytes == 1024**3
    assert subscription.whitelist_usage_cursor_bytes == 101 * 1024**3


@pytest.mark.asyncio
async def test_admin_whitelist_grant_is_logged_and_idempotent(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=14109,
            username="wl_admin_grant",
            first_name="Admin",
            last_name="Grant",
            language_code="ru",
        )
        admin = await hub.accounts.create_admin(
            username="grant_admin",
            password="password",
            full_name="Grant Admin",
            telegram_id=9914109,
        )
        first = await hub.billing.grant_whitelist_traffic(
            user.id,
            gigabytes=Decimal("12.5"),
            request_key="admin-grant-once",
            admin_id=admin.id,
            reason="Компенсация",
        )
        repeated = await hub.billing.grant_whitelist_traffic(
            user.id,
            gigabytes=Decimal("12.5"),
            request_key="admin-grant-once",
            admin_id=admin.id,
            reason="Компенсация",
        )
        history = await hub.billing.list_whitelist_package_purchases(user.id)

    assert first.id == repeated.id
    assert user.whitelist_extra_traffic_bytes == int(Decimal("12.5") * Decimal(1024**3))
    assert len(history) == 1
    assert history[0].created_by_admin_id == admin.id
    assert history[0].reason == "Компенсация"


@pytest.mark.asyncio
async def test_start_renewal_charge_does_not_repeat_whitelist_usage_that_was_already_charged(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13022,
            username="start_renewal_charge",
            first_name="Start",
            last_name="Renewal",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        subscription.whitelist_billing_version = 1
        subscription.whitelist_included_limit_bytes = 0
        whitelist_node = next(node for node in test_services.remnawave.nodes.values() if "Whitelist" in node.name)

        test_services.remnawave.set_usage(
            user.remnawave_user_uuid,
            used_bytes=2 * 1024**3,
            lifetime_used_bytes=2 * 1024**3,
        )
        test_services.remnawave.set_node_usage(
            whitelist_node.uuid,
            user.remnawave_user_uuid,
            2 * 1024**3,
        )

        await hub.billing.snapshot_traffic()
        refreshed_user = await hub.accounts.get_user(user.id)
        refreshed_subscription = await hub.accounts.get_current_subscription(user.id)

        renewal_charge = hub.billing._compute_renewal_charge(refreshed_subscription, refreshed_subscription.plan)

    assert refreshed_subscription is not None
    assert Decimal(refreshed_user.balance_rub) == Decimal("27.00")
    assert renewal_charge == SINGLE_10GBIT_MONTHLY_PRICE_RUB


@pytest.mark.asyncio
async def test_renewal_reminders_require_enabled_autorenew_and_insufficient_balance(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13037,
            username="renewal_reminder_balance",
            first_name="Renewal",
            last_name="Reminder",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("200"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(
            user.id,
            PlanCode.SINGLE_10GBIT,
            charge_user=True,
        )
        subscription.ends_at = utc_now() + timedelta(hours=20)
        subscription.next_billing_at = subscription.ends_at
        user.balance_rub = Decimal(subscription.plan.price_rub)

        await hub.billing.process_due_subscriptions()
        sufficient_balance_notifications = list(
            (await hub.session.scalars(await hub.notifications.pending_query(limit=30))).all()
        )

        user.balance_rub = Decimal("0")
        subscription.auto_renew = False
        await hub.billing.process_due_subscriptions()
        disabled_autorenew_notifications = list(
            (await hub.session.scalars(await hub.notifications.pending_query(limit=30))).all()
        )
        disabled_autorenew_notice = next(
            item
            for item in disabled_autorenew_notifications
            if item.user_id == user.id
            and item.type == NotificationType.BROADCAST
            and item.payload.get("kind") == "renewal_disabled_expiring"
        )

        subscription.auto_renew = True
        await hub.billing.process_due_subscriptions()
        insufficient_balance_notifications = list(
            (await hub.session.scalars(await hub.notifications.pending_query(limit=30))).all()
        )

    reminder_types = {NotificationType.UPCOMING_RENEWAL, NotificationType.LOW_BALANCE}
    assert not any(
        item.user_id == user.id and item.type in reminder_types
        for item in sufficient_balance_notifications
    )
    assert not any(
        item.user_id == user.id and item.type in reminder_types
        for item in disabled_autorenew_notifications
    )
    assert "Автопродление сейчас отключено" in disabled_autorenew_notice.message
    assert disabled_autorenew_notice.payload["cta"] == "renewal_disabled_expiring"
    low_balance_notice = next(
        item
        for item in insufficient_balance_notifications
        if item.user_id == user.id and item.type == NotificationType.LOW_BALANCE
    )
    assert not any(
        item.user_id == user.id and item.type == NotificationType.UPCOMING_RENEWAL
        for item in insufficient_balance_notifications
    )
    assert low_balance_notice.payload["cta"] == "low_balance"
    assert "Не хватает средств" in low_balance_notice.message
    assert "Текущий баланс: 0.00 ₽" in low_balance_notice.message
    assert f"К списанию: {Decimal(subscription.plan.price_rub):.2f} ₽" in low_balance_notice.message


@pytest.mark.asyncio
async def test_disabled_autorenew_expiration_notifies_and_resume_reactivates_same_plan(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13038,
            username="disabled_autorenew_expired",
            first_name="Expired",
            last_name="Renewal",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("200"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(
            user.id,
            PlanCode.SINGLE_10GBIT,
            charge_user=True,
        )
        balance_before_expiration = Decimal(user.balance_rub)
        subscription.auto_renew = False
        subscription.ends_at = utc_now() - timedelta(minutes=1)
        subscription.next_billing_at = subscription.ends_at

        await hub.billing.process_due_subscriptions()

        assert await hub.accounts.get_current_subscription(user.id) is None
        expired_subscription = await hub.accounts.get_latest_paid_subscription(user.id)
        assert expired_subscription is not None
        assert expired_subscription.status == SubscriptionStatus.CANCELED
        assert Decimal(user.balance_rub) == balance_before_expiration
        ended_notices = [
            item
            for item in (await hub.session.scalars(await hub.notifications.pending_query(limit=30))).all()
            if item.user_id == user.id
            and item.payload
            and item.payload.get("kind") == "subscription_ended_auto_renew_disabled"
        ]
        assert len(ended_notices) == 1
        assert ended_notices[0].payload["cta"] == "subscription_ended_auto_renew_disabled"

        resumed_subscription = await hub.billing.restore_subscription_renewal(user.id)

        assert resumed_subscription.id != expired_subscription.id
        assert resumed_subscription.plan.code == PlanCode.SINGLE_10GBIT
        assert resumed_subscription.status == SubscriptionStatus.ACTIVE
        assert resumed_subscription.auto_renew is True
        assert Decimal(user.balance_rub) == balance_before_expiration - SINGLE_10GBIT_MONTHLY_PRICE_RUB


@pytest.mark.asyncio
async def test_auto_renewal_consumes_one_time_discount_and_syncs_future_remnawave_expiration(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13032,
            username="discounted_auto_renewal",
            first_name="Discounted",
            last_name="Renewal",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(
            user.id,
            PlanCode.SINGLE_10GBIT,
            charge_user=True,
        )
        promo = await hub.promos.create_code(
            code="RENEW10",
            name="Renewal discount",
            reward_kind=PromoRewardKind.PLAN_DISCOUNT,
            reward_value=Decimal("10"),
            usage_limit=1,
            expires_at=None,
            new_users_only=False,
            admin_id=None,
            assigned_user_id=user.id,
        )
        _, redemption, _ = await hub.promos.redeem_code(user.id, promo.code)

        subscription.ends_at = utc_now() - timedelta(days=45)
        subscription.next_billing_at = subscription.ends_at
        balance_before = Decimal(user.balance_rub)
        renewal_started_after = utc_now()

        await hub.billing.process_due_subscriptions()

        refreshed_user = await hub.accounts.get_user(user.id)
        refreshed_subscription = await hub.accounts.get_current_subscription(user.id)
        refreshed_redemption = await hub.session.get(PromoCodeRedemption, redemption.id)
        remote_user = await test_services.remnawave.get_user(user.remnawave_user_uuid)
        renewal_transactions = list(
            (
                await hub.session.scalars(
                    select(BalanceTransaction)
                    .where(
                        BalanceTransaction.user_id == user.id,
                        BalanceTransaction.type == BalanceTransactionType.SUBSCRIPTION_CHARGE,
                    )
                    .order_by(BalanceTransaction.created_at.asc())
                )
            ).all()
        )

    assert refreshed_subscription is not None
    assert ensure_utc(refreshed_subscription.started_at) >= renewal_started_after
    assert ensure_utc(refreshed_subscription.ends_at) > utc_now()
    assert ensure_utc(remote_user.expireAt) == ensure_utc(refreshed_subscription.ends_at)
    assert Decimal(refreshed_user.balance_rub) == balance_before - Decimal("62.10")
    assert refreshed_redemption is not None
    assert refreshed_redemption.applied_at is not None
    assert refreshed_redemption.applied_subscription_id == refreshed_subscription.id
    assert refreshed_redemption.reward_value_applied == Decimal("6.90")
    assert renewal_transactions[-1].amount_rub == Decimal("-62.10")
    assert "RENEW10" in (renewal_transactions[-1].description or "")


@pytest.mark.asyncio
async def test_auto_renewal_extends_remnawave_before_current_access_expires(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13034,
            username="seamless_auto_renewal",
            first_name="Seamless",
            last_name="Renewal",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(
            user.id,
            PlanCode.SINGLE_10GBIT,
            charge_user=True,
        )
        previous_ends_at = utc_now() + timedelta(minutes=10)
        subscription.ends_at = previous_ends_at
        subscription.next_billing_at = previous_ends_at
        balance_before = Decimal(user.balance_rub)

        await hub.billing.process_due_subscriptions()

        refreshed_subscription = await hub.accounts.get_current_subscription(user.id)
        remote_user = await test_services.remnawave.get_user(user.remnawave_user_uuid)

    assert refreshed_subscription is not None
    assert ensure_utc(refreshed_subscription.started_at) == ensure_utc(previous_ends_at)
    assert ensure_utc(refreshed_subscription.ends_at) > ensure_utc(previous_ends_at)
    assert ensure_utc(remote_user.expireAt) == ensure_utc(refreshed_subscription.ends_at)
    assert Decimal(user.balance_rub) == balance_before - Decimal("69.00")


@pytest.mark.asyncio
async def test_early_renewal_window_does_not_block_user_without_enough_balance(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13035,
            username="early_renewal_low_balance",
            first_name="Early",
            last_name="Low Balance",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(
            user.id,
            PlanCode.SINGLE_10GBIT,
            charge_user=True,
        )
        previous_ends_at = utc_now() + timedelta(minutes=10)
        subscription.ends_at = previous_ends_at
        subscription.next_billing_at = previous_ends_at
        user.balance_rub = Decimal("0.00")

        await hub.billing.process_due_subscriptions()

        refreshed_subscription = await hub.accounts.get_current_subscription(user.id)

    assert refreshed_subscription is not None
    assert refreshed_subscription.status == SubscriptionStatus.ACTIVE
    assert user.status == UserStatus.ACTIVE
    assert ensure_utc(refreshed_subscription.ends_at) == ensure_utc(previous_ends_at)


@pytest.mark.asyncio
async def test_consumed_discount_is_not_reused_on_second_automatic_renewal(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13033,
            username="single_use_auto_renewal",
            first_name="Single",
            last_name="Use",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        subscription = await hub.billing.activate_paid_plan(
            user.id,
            PlanCode.SINGLE_10GBIT,
            charge_user=True,
        )
        promo = await hub.promos.create_code(
            code="ONCE10",
            name="Single renewal discount",
            reward_kind=PromoRewardKind.PLAN_DISCOUNT,
            reward_value=Decimal("10"),
            usage_limit=1,
            expires_at=None,
            new_users_only=False,
            admin_id=None,
            assigned_user_id=user.id,
        )
        await hub.promos.redeem_code(user.id, promo.code)

        subscription.ends_at = utc_now() - timedelta(minutes=1)
        subscription.next_billing_at = subscription.ends_at
        await hub.billing.process_due_subscriptions()
        balance_after_discounted_renewal = Decimal(user.balance_rub)

        subscription.ends_at = utc_now() - timedelta(minutes=1)
        subscription.next_billing_at = subscription.ends_at
        await hub.billing.process_due_subscriptions()

        refreshed_user = await hub.accounts.get_user(user.id)

    assert balance_after_discounted_renewal - Decimal(refreshed_user.balance_rub) == Decimal("69.00")


@pytest.mark.asyncio
async def test_failed_plan_switch_does_not_credit_balance_or_cancel_current_subscription(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13023,
            username="failed_switch_credit",
            first_name="Failed",
            last_name="Switch",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("100"), auto_complete=True)
        current = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        current.ends_at = utc_now() + timedelta(days=15)
        current.next_billing_at = current.ends_at
        current.started_at = utc_now() - timedelta(days=15)
        user.balance_rub = Decimal("-10.00")
        balance_before = Decimal(user.balance_rub)

        with pytest.raises(ConflictError) as exc_info:
            await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED, charge_user=True)

        refreshed_user = await hub.accounts.get_user(user.id)
        refreshed_current = await hub.accounts.get_current_subscription(user.id)
        refunds = list(
            (
                await hub.session.scalars(
                    select(BalanceTransaction).where(
                        BalanceTransaction.user_id == user.id,
                        BalanceTransaction.type == BalanceTransactionType.REFUND,
                    )
                )
            ).all()
        )

    assert "Недостаточно средств" in str(exc_info.value)
    assert Decimal(refreshed_user.balance_rub) == balance_before
    assert refreshed_current is not None
    assert refreshed_current.id == current.id
    assert refreshed_current.status == SubscriptionStatus.ACTIVE
    assert refunds == []


@pytest.mark.asyncio
async def test_plan_switch_carries_included_whitelist_usage_and_extra_package(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13024,
            username="switch_whitelist_usage",
            first_name="Switch",
            last_name="Whitelist",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("1000"), auto_complete=True)
        previous = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        previous.whitelist_traffic_used_bytes = 4 * 1024**3
        await hub.billing._apply_instant_whitelist_charges(user, previous)
        user.whitelist_extra_traffic_bytes = 25 * 1024**3

        switched = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED)
        status = await hub.billing.get_whitelist_traffic_status(user.id)

    assert previous.status == SubscriptionStatus.CANCELED
    assert switched.whitelist_included_limit_bytes == 50 * 1024**3
    assert switched.whitelist_included_consumed_bytes == 4 * 1024**3
    assert switched.whitelist_traffic_used_bytes == 0
    assert switched.whitelist_traffic_accounted_bytes == 0
    assert status.included_remaining_bytes == 46 * 1024**3
    assert user.whitelist_extra_traffic_bytes == 25 * 1024**3


@pytest.mark.asyncio
async def test_plan_downgrade_and_upgrade_cannot_restore_consumed_whitelist_limit(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13026,
            username="switch_whitelist_chain",
            first_name="Switch",
            last_name="Chain",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("1000"), auto_complete=True)
        pro = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED)
        pro.whitelist_traffic_used_bytes = 50 * 1024**3
        await hub.billing._apply_instant_whitelist_charges(user, pro)

        start = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        start_status = await hub.billing.get_whitelist_traffic_status(user.id)
        pro_again = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED)
        pro_status = await hub.billing.get_whitelist_traffic_status(user.id)

    assert start.whitelist_included_consumed_bytes == 50 * 1024**3
    assert start_status.included_remaining_bytes == 0
    assert pro_again.whitelist_included_consumed_bytes == 50 * 1024**3
    assert pro_status.included_remaining_bytes == 0


@pytest.mark.asyncio
async def test_paid_whitelist_usage_is_not_mistaken_for_included_usage_after_upgrade(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13027,
            username="switch_paid_whitelist",
            first_name="Paid",
            last_name="Whitelist",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("1000"), auto_complete=True)
        start = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        balance_before_usage = Decimal(user.balance_rub)
        start.whitelist_traffic_used_bytes = 7 * 1024**3
        await hub.billing._apply_instant_whitelist_charges(user, start)
        balance_after_usage = Decimal(user.balance_rub)

        pro = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED)
        status = await hub.billing.get_whitelist_traffic_status(user.id)

    assert balance_after_usage == balance_before_usage - Decimal("4.00")
    assert pro.whitelist_included_consumed_bytes == 5 * 1024**3
    assert status.included_remaining_bytes == 45 * 1024**3


@pytest.mark.asyncio
async def test_partially_used_package_survives_switch_and_renewal(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13028,
            username="switch_package_renewal",
            first_name="Package",
            last_name="Renewal",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("1000"), auto_complete=True)
        start = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        await hub.billing.purchase_whitelist_package(user.id, "25", request_key="switch-package")
        start.whitelist_traffic_used_bytes = 10 * 1024**3
        await hub.billing._apply_instant_whitelist_charges(user, start)

        pro = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED)
        status_after_switch = await hub.billing.get_whitelist_traffic_status(user.id)
        await hub.billing._start_next_billing_cycle(pro, pro.plan, renewed_at=utc_now())
        status_after_renewal = await hub.billing.get_whitelist_traffic_status(user.id)

    assert status_after_switch.included_remaining_bytes == 45 * 1024**3
    assert status_after_switch.extra_remaining_bytes == 20 * 1024**3
    assert status_after_renewal.included_remaining_bytes == 50 * 1024**3
    assert status_after_renewal.extra_remaining_bytes == 20 * 1024**3


@pytest.mark.asyncio
async def test_switching_from_legacy_period_starts_new_billing_without_old_usage(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13029,
            username="legacy_plan_switch",
            first_name="Legacy",
            last_name="Switch",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("1000"), auto_complete=True)
        legacy = await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED)
        legacy.whitelist_billing_version = 1
        legacy.whitelist_traffic_used_bytes = 120 * 1024**3

        switched = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT_WEEKLY)
        status = await hub.billing.get_whitelist_traffic_status(user.id)

    assert switched.whitelist_billing_version == WHITELIST_BILLING_VERSION
    assert switched.whitelist_included_consumed_bytes == 0
    assert status.included_limit_bytes == 1024**3
    assert status.included_remaining_bytes == 1024**3


@pytest.mark.asyncio
async def test_reactivating_current_plan_is_rejected_without_charging(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13025,
            username="same_plan_switch",
            first_name="Same",
            last_name="Plan",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)
        current = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)
        balance_before = Decimal(user.balance_rub)

        with pytest.raises(ConflictError, match="уже активен"):
            await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT)

        refreshed = await hub.accounts.get_current_subscription(user.id)

    assert refreshed is not None
    assert refreshed.id == current.id
    assert refreshed.status == SubscriptionStatus.ACTIVE
    assert Decimal(user.balance_rub) == balance_before


@pytest.mark.asyncio
async def test_activate_paid_plan_ignores_unrelated_remote_user_sync_failure(
    test_services,
    monkeypatch,
):
    async with test_services.hub() as hub:
        broken_user = await hub.accounts.get_or_create_user(
            telegram_id=13024,
            username="broken_remote_sync",
            first_name="Broken",
            last_name="Remote",
            language_code="ru",
        )
        await hub.topups.create_request(broken_user.id, Decimal("500"), auto_complete=True)
        await hub.billing.activate_paid_plan(broken_user.id, PlanCode.SINGLE_10GBIT, charge_user=True)

        user = await hub.accounts.get_or_create_user(
            telegram_id=13025,
            username="activation_survives_other_user_failure",
            first_name="Activation",
            last_name="Survives",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)

        original_update_user = test_services.remnawave.update_user

        async def flaky_update_user(payload: dict):
            if payload.get("telegramId") == broken_user.telegram_id:
                raise httpx.ConnectError("temporary remote sync failure")
            return await original_update_user(payload)

        monkeypatch.setattr(test_services.remnawave, "update_user", flaky_update_user)

        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        refreshed_user = await hub.accounts.get_user(user.id)
        remote_user = await test_services.remnawave.get_user(refreshed_user.remnawave_user_uuid)

    assert subscription.status == SubscriptionStatus.ACTIVE
    assert remote_user.activeInternalSquads


@pytest.mark.asyncio
async def test_new_paid_user_activation_creates_remnawave_user_and_subscription_link(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13031,
            username="new_paid_remote_user",
            first_name="New",
            last_name="Paid",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)

        subscription = await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)
        refreshed_user = await hub.accounts.get_user(user.id)
        bundle = await hub.accounts.get_subscription_bundle(user.id)
        remote_user = await test_services.remnawave.get_user(refreshed_user.remnawave_user_uuid)

    assert subscription.status == SubscriptionStatus.ACTIVE
    assert refreshed_user.remnawave_user_uuid
    assert refreshed_user.remnawave_short_uuid
    assert refreshed_user.remnawave_username
    assert remote_user.telegramId == 13031
    assert remote_user.status == "ACTIVE"
    assert remote_user.activeInternalSquads
    assert bundle["subscription_url"]
    assert refreshed_user.remnawave_short_uuid in bundle["subscription_url"]


@pytest.mark.asyncio
async def test_activate_paid_plan_wraps_panel_connection_errors_as_conflict(test_services, monkeypatch):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13026,
            username="panel_unavailable",
            first_name="Panel",
            last_name="Unavailable",
            language_code="ru",
        )
        await hub.topups.create_request(user.id, Decimal("500"), auto_complete=True)

        async def broken_create_user(payload: dict):
            raise httpx.ConnectError("panel unavailable")

        monkeypatch.setattr(test_services.remnawave, "create_user", broken_create_user)

        with pytest.raises(ConflictError) as exc_info:
            await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=True)

    assert "Попробуйте позже" in str(exc_info.value)


@pytest.mark.asyncio
async def test_sync_users_with_available_nodes_restores_remote_squads(test_services):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13027,
            username="node_access_restore",
            first_name="Node",
            last_name="Restore",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)
        user = await hub.accounts.get_user(user.id)
        remote = await test_services.remnawave.get_user(user.remnawave_user_uuid)
        remote.activeInternalSquads = []

        summary = await hub.billing.sync_users_with_available_nodes()
        refreshed_user = await hub.accounts.get_user(user.id)
        active_accesses = await hub.catalog.get_user_servers(user.id)
        remote = await test_services.remnawave.get_user(refreshed_user.remnawave_user_uuid)

    expected_squad_ids = {
        access.server.remnawave_internal_squad_uuid
        for access in active_accesses
        if access.server and access.server.remnawave_internal_squad_uuid
    }
    remote_squad_ids = {item.uuid for item in remote.activeInternalSquads}

    assert summary["total"] == 1
    assert summary["synced"] == 1
    assert summary["failed"] == 0
    assert remote_squad_ids == expected_squad_ids
    assert remote_squad_ids


@pytest.mark.asyncio
async def test_sync_users_with_available_nodes_ignores_unavailable_server_when_catalog_sync_fails(
    test_services,
    monkeypatch,
):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13032,
            username="node_access_unavailable",
            first_name="Node",
            last_name="Unavailable",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)
        user = await hub.accounts.get_user(user.id)
        disabled_access = next(access for access in await hub.catalog.get_user_servers(user.id) if access.server)
        disabled_server_id = disabled_access.server_id
        disabled_squad_uuid = disabled_access.server.remnawave_internal_squad_uuid
        disabled_access.server.is_connected = False

    async with test_services.hub() as hub:
        async def broken_catalog_sync():
            raise RuntimeError("node API is temporarily unavailable")

        monkeypatch.setattr(hub.catalog, "sync_servers", broken_catalog_sync)

        summary = await hub.billing.sync_users_with_available_nodes()
        user = await hub.accounts.get_user(user.id)
        active_accesses = await hub.catalog.get_user_servers(user.id)
        remote = await test_services.remnawave.get_user(user.remnawave_user_uuid)

    expected_squad_ids = {
        access.server.remnawave_internal_squad_uuid
        for access in active_accesses
        if access.server
        and access.server_id != disabled_server_id
        and access.server.remnawave_internal_squad_uuid
        and access.server.is_connected
    }
    remote_squad_ids = {item.uuid for item in remote.activeInternalSquads}

    assert summary["catalog_synced"] is False
    assert summary["synced"] == 1
    assert summary["failed"] == 0
    assert disabled_squad_uuid not in remote_squad_ids
    assert remote_squad_ids == expected_squad_ids
    assert remote_squad_ids


@pytest.mark.asyncio
async def test_sync_users_with_available_nodes_skips_locally_expired_subscription(test_services, monkeypatch):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13033,
            username="expired_node_access",
            first_name="Expired",
            last_name="Access",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)
        subscription = await hub.session.scalar(select(Subscription).where(Subscription.user_id == user.id))
        subscription.ends_at = utc_now() - timedelta(hours=1)

        async def fail_update_user(payload: dict):
            raise AssertionError("expired subscriptions must not be sent to Remnawave")

        monkeypatch.setattr(test_services.remnawave, "update_user", fail_update_user)

        summary = await hub.billing.sync_users_with_available_nodes()

    assert summary["total"] == 1
    assert summary["skipped"] == 1
    assert summary["failed"] == 0


@pytest.mark.asyncio
async def test_sync_users_with_available_nodes_reports_progress(test_services):
    progress_events: list[dict[str, object]] = []

    async def collect_progress(payload: dict[str, object]) -> None:
        progress_events.append(dict(payload))

    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13031,
            username="node_access_progress",
            first_name="Node",
            last_name="Progress",
            language_code="ru",
        )
        await hub.billing.activate_trial(user.id)

        summary = await hub.billing.sync_users_with_available_nodes(progress_callback=collect_progress)

    stages = [str(item["stage"]) for item in progress_events]

    assert summary["total"] == 1
    assert stages[0] == "catalog"
    assert "users_loaded" in stages
    assert "user_processed" in stages
    assert stages[-1] == "completed"
    assert progress_events[-1]["processed"] == summary["total"]


@pytest.mark.asyncio
async def test_sync_users_with_available_nodes_recreates_missing_remote_user(test_services, monkeypatch):
    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=13028,
            username="node_access_recreate",
            first_name="Node",
            last_name="Recreate",
            language_code="ru",
        )
        await hub.billing.activate_paid_plan(user.id, PlanCode.SINGLE_10GBIT, charge_user=False)
        user = await hub.accounts.get_user(user.id)
        old_remote_uuid = user.remnawave_user_uuid
        user.remnawave_user_uuid = "missing-remote-user"
        user.remnawave_username = "old_remote"
        user.remnawave_short_uuid = "old-short"

        original_update_user = test_services.remnawave.update_user

        async def missing_update_user(payload: dict):
            if payload.get("uuid") == "missing-remote-user":
                request = httpx.Request("PATCH", "https://remna.example/api/users")
                response = httpx.Response(404, request=request, json={"message": "not found"})
                raise httpx.HTTPStatusError("not found", request=request, response=response)
            return await original_update_user(payload)

        monkeypatch.setattr(test_services.remnawave, "update_user", missing_update_user)

        summary = await hub.billing.sync_users_with_available_nodes()
        refreshed_user = await hub.accounts.get_user(user.id)
        remote = await test_services.remnawave.get_user(refreshed_user.remnawave_user_uuid)

    assert summary["total"] == 1
    assert summary["synced"] == 1
    assert summary["recreated"] == 1
    assert summary["failed"] == 0
    assert refreshed_user.remnawave_user_uuid not in {old_remote_uuid, "missing-remote-user"}
    assert refreshed_user.remnawave_short_uuid != "old-short"
    assert remote.telegramId == 13028
    assert remote.activeInternalSquads


@pytest.mark.asyncio
async def test_sync_users_with_available_nodes_keeps_processing_after_user_error(test_services, monkeypatch):
    async with test_services.hub() as hub:
        ok_user = await hub.accounts.get_or_create_user(
            telegram_id=13029,
            username="node_access_ok",
            first_name="Node",
            last_name="Ok",
            language_code="ru",
        )
        broken_user = await hub.accounts.get_or_create_user(
            telegram_id=13030,
            username="node_access_broken",
            first_name="Node",
            last_name="Broken",
            language_code="ru",
        )
        await hub.billing.activate_trial(ok_user.id)
        await hub.billing.activate_trial(broken_user.id)

        original_update_user = test_services.remnawave.update_user

        async def flaky_update_user(payload: dict):
            if payload.get("telegramId") == 13030:
                raise httpx.ConnectError("temporary panel error")
            return await original_update_user(payload)

        monkeypatch.setattr(test_services.remnawave, "update_user", flaky_update_user)

        summary = await hub.billing.sync_users_with_available_nodes()
        ok_user = await hub.accounts.get_user(ok_user.id)
        remote = await test_services.remnawave.get_user(ok_user.remnawave_user_uuid)

    assert summary["total"] == 2
    assert summary["synced"] == 1
    assert summary["failed"] == 1
    assert summary["errors"]
    assert remote.activeInternalSquads
