from __future__ import annotations

from decimal import Decimal

from altlink.presentation.web.helpers import format_bytes, format_dt


def money(value: Decimal) -> str:
    return f"{value:.2f} ₽"


def profile_text(summary: dict) -> str:
    user = summary["user"]
    subscription = summary["subscription"]
    plan = summary["plan"]
    lines = [
        "Ваш профиль",
        f"Telegram ID: {user.telegram_id}",
        f"Статус: {user.status.value}",
        f"Баланс: {money(user.balance_rub)}",
    ]
    if plan and subscription:
        lines.extend(
            [
                f"Тариф: {plan.name_ru}",
                f"Следующее продление: {format_dt(subscription.next_billing_at)}",
                f"Задолженность: {money(subscription.debt_rub)}",
                f"Трафик за период: {format_bytes(subscription.traffic_used_bytes_cache)}",
            ]
        )
    else:
        lines.append("Подписка ещё не активирована.")
    return "\n".join(lines)


def subscription_text(summary: dict) -> str:
    subscription = summary["subscription"]
    plan = summary["plan"]
    if not subscription or not plan:
        return "Подписка ещё не активирована. Вы можете получить тестовый период или выбрать тариф."
    return "\n".join(
        [
            "Моя подписка",
            f"Тариф: {plan.name_ru}",
            f"Статус: {subscription.status.value}",
            f"Период: {format_dt(subscription.current_period_start)} → {format_dt(subscription.current_period_end)}",
            f"Следующее продление: {format_dt(subscription.next_billing_at)}",
            f"Задолженность: {money(subscription.debt_rub)}",
            f"Трафик: {format_bytes(subscription.traffic_used_bytes_cache)}",
        ]
    )

