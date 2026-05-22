from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from altlink.domain.enums import PlanCode, SubscriptionStatus
from altlink.presentation.bots import client_handlers
from altlink.settings import Settings


def test_home_text_for_expired_trial_explains_next_steps():
    settings = Settings(_env_file=None, backend_public_url="https://altlink.online")
    user = SimpleNamespace(balance_rub=Decimal("0.00"), status="blocked")
    latest_subscription = SimpleNamespace(
        status=SubscriptionStatus.EXPIRED,
        plan=SimpleNamespace(name="Trial", is_trial=True, code=PlanCode.TRIAL),
    )

    text = client_handlers.home_text(user, None, settings, latest_subscription=latest_subscription)

    assert "Тестовый период завершён" in text
    assert "1. Откройте «Подписка»." in text
    assert "2. Нажмите «Выбрать тариф»." in text


def test_subscription_text_for_expired_trial_points_user_to_paid_plan():
    settings = Settings(_env_file=None, backend_public_url="https://altlink.online")
    user = SimpleNamespace(balance_rub=Decimal("0.00"), status="blocked")
    latest_subscription = SimpleNamespace(
        status=SubscriptionStatus.EXPIRED,
        plan=SimpleNamespace(name="Trial", is_trial=True, code=PlanCode.TRIAL),
    )

    text = client_handlers.subscription_text(
        {"user": user, "subscription": None},
        user_servers=[],
        settings=settings,
        latest_subscription=latest_subscription,
    )

    assert "Пробный период уже завершён." in text
    assert "активируйте Start или Pro" in text
