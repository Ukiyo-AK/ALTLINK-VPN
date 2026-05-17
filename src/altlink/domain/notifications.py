from __future__ import annotations

from datetime import datetime
from decimal import Decimal


def rub(amount: Decimal | int | float) -> str:
    return f"{Decimal(amount):.2f} ₽"


def format_datetime(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


def low_balance_message(
    balance: Decimal,
    next_charge: Decimal,
    due_at: datetime,
    reminder_window: str,
) -> str:
    return (
        "⚠️ Баланс почти закончился.\n\n"
        f"До следующего списания осталось {reminder_window}.\n"
        f"Сейчас на счёте: {rub(balance)}\n"
        f"Ближайшее списание: {rub(next_charge)}\n"
        f"Дата списания: {format_datetime(due_at)}\n\n"
        "Пополните баланс заранее, чтобы доступ не прервался."
    )


def upcoming_renewal_message(next_charge: Decimal, due_at: datetime) -> str:
    return (
        "📅 Скоро будет следующее списание.\n\n"
        f"К списанию: {rub(next_charge)}\n"
        f"Дата: {format_datetime(due_at)}"
    )


def grace_started_message(balance: Decimal, debt: Decimal, grace_until: datetime) -> str:
    return (
        "⏳ Подписка перешла в льготный период.\n\n"
        f"Баланс: {rub(balance)}\n"
        f"Текущая задолженность: {rub(debt)}\n"
        f"Доступ сохранится до: {format_datetime(grace_until)}\n\n"
        "Пополните баланс, чтобы не потерять доступ."
    )


def grace_reminder_message(debt: Decimal, grace_until: datetime) -> str:
    return (
        "🔔 Напоминание о задолженности.\n\n"
        f"Нужно пополнить: {rub(debt)}\n"
        f"Льготный период закончится: {format_datetime(grace_until)}"
    )


def blocked_message() -> str:
    return (
        "🚫 Доступ к VPN заблокирован.\n\n"
        "Льготный период закончился. Пополните баланс и возобновите тариф, чтобы снова получить доступ."
    )


def topup_approved_message(amount: Decimal) -> str:
    return f"✅ Платёж подтверждён. На баланс зачислено {rub(amount)}."


def topup_rejected_message(amount: Decimal, comment: str | None = None) -> str:
    suffix = f"\nКомментарий: {comment}" if comment else ""
    return f"❌ Платёж на сумму {rub(amount)} не прошёл.{suffix}"


def trial_ended_message() -> str:
    return (
        "⌛ Тестовый период завершён.\n\n"
        "Чтобы продолжить пользоваться VPN, выберите тариф и пополните баланс."
    )


def trial_expiring_message(ends_at: datetime, reminder_window: str) -> str:
    return (
        "⏳ Пробный период скоро закончится.\n\n"
        f"До окончания осталось {reminder_window}.\n"
        f"Доступ завершится: {format_datetime(ends_at)}\n\n"
        "Выберите платный тариф заранее, чтобы не потерять доступ."
    )


def inactive_subscription_promo_message(promo_code: str = "ALT10", discount_percent: int = 10) -> str:
    return (
        "🎁 Для вас есть скидка на первый платный тариф.\n\n"
        f"Используйте промокод {promo_code} и получите {discount_percent}% скидки.\n"
        "Откройте раздел «Подписка», выберите тариф и примените промокод при покупке.\n\n"
        "✨ Start — для повседневного использования.\n"
        "🚀 Pro — для максимальной скорости и всех доступных серверов."
    )


def traffic_threshold_message(percent: int, used_gb: float, limit_gb: float) -> str:
    return (
        f"📊 Использовано {percent}% лимита трафика.\n\n"
        f"Израсходовано: {used_gb:.2f} ГБ из {limit_gb:.2f} ГБ."
    )


def traffic_exceeded_message(limit_gb: float) -> str:
    return (
        f"⛔ Лимит трафика {limit_gb:.2f} ГБ исчерпан. "
        "Доступ временно остановлен до начала нового оплаченного периода."
    )
