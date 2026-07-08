from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape

from altlink.utils.time import format_msk_datetime


def rub(amount: Decimal | int | float) -> str:
    return f"{Decimal(amount):.2f} ₽"


def format_datetime(dt: datetime) -> str:
    return format_msk_datetime(dt)


def low_balance_message(
    balance: Decimal,
    next_charge: Decimal,
    due_at: datetime,
    reminder_window: str,
) -> str:
    return (
        "⚠️ Не хватает средств для автопродления.\n\n"
        f"До следующего списания осталось {reminder_window}.\n"
        f"Текущий баланс: {rub(balance)}\n"
        f"К списанию: {rub(next_charge)}\n"
        f"Дата списания: {format_datetime(due_at)}\n\n"
        "Пополните баланс до этой даты, чтобы доступ не прервался."
    )


def upcoming_renewal_message(
    balance: Decimal,
    next_charge: Decimal,
    due_at: datetime,
) -> str:
    return (
        "📅 Скоро будет следующее списание.\n\n"
        f"Текущий баланс: {rub(balance)}\n"
        f"К списанию: {rub(next_charge)}\n"
        f"Дата: {format_datetime(due_at)}"
    )


def renewal_disabled_expiring_message(
    balance: Decimal,
    next_charge: Decimal,
    due_at: datetime,
) -> str:
    return (
        "⚠️ Срок действия подписки скоро истечёт.\n\n"
        "Автопродление сейчас отключено.\n"
        f"Подписка действует до: {format_datetime(due_at)}\n"
        f"Баланс: {rub(balance)}\n"
        f"Для продления потребуется: {rub(next_charge)}\n\n"
        "Пополните баланс при необходимости и включите автопродление, чтобы не потерять доступ."
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


def blocked_message(*, grace_ended: bool = False) -> str:
    reason = (
        "Льготный период закончился. Пополните баланс и возобновите тариф, чтобы снова получить доступ."
        if grace_ended
        else "Подписка не была продлена. Пополните баланс и возобновите тариф, чтобы снова получить доступ."
    )
    return (
        "🚫 Доступ к VPN заблокирован.\n\n"
        f"{reason}"
    )


def topup_approved_message(amount: Decimal) -> str:
    return f"✅ Платёж подтверждён. На баланс зачислено {rub(amount)}."


def topup_rejected_message(amount: Decimal, comment: str | None = None) -> str:
    suffix = f"\nКомментарий: {comment}" if comment else ""
    return f"❌ Не удалось оплатить {rub(amount)}. Попробуйте ещё раз.{suffix}"


def trial_ended_message() -> str:
    return (
        "⌛ Тестовый период завершён.\n\n"
        "Чтобы продолжить пользоваться ускорителем, откройте раздел «Подписка» и выберите платный тариф.\n"
        "Если на балансе не хватает средств, сначала пополните его кнопкой ниже."
    )


def trial_expiring_message(ends_at: datetime, reminder_window: str) -> str:
    return (
        "⏳ Пробный период скоро закончится.\n\n"
        f"До окончания осталось {reminder_window}.\n"
        f"Доступ завершится: {format_datetime(ends_at)}\n\n"
        "Чтобы не потерять доступ, заранее откройте «Подписка» и выберите тариф."
    )


PROMO_MESSAGE_TEMPLATES: dict[int, dict[str, str]] = {
    1: {
        "kind": "discount",
        "text": (
            "<b>Интернет снова чудит? ALTLINK VPN поможет ⚡</b>\n\n"
            "Если сайты не открываются, соединение режется или интернет работает только по белым спискам — "
            "самое время вернуться к <b>ALTLINK VPN</b>.\n\n"
            "С нами привычные сервисы остаются доступными, а подключение занимает меньше минуты.\n\n"
            "Используйте промокод <code>{PROMO}</code> и получите скидку <b>{DISCOUNT}%</b> 👇"
        ),
    },
    2: {
        "kind": "trial",
        "text": (
            "<b>Когда обычный интернет подводит — включайте ALTLINK VPN 🔐</b>\n\n"
            "Глушат интернет, режут соединения или часть сервисов просто не открывается?\n"
            "<b>ALTLINK VPN</b> поможет оставаться онлайн даже тогда, когда всё вокруг работает нестабильно.\n\n"
            "Для возвращения дарим вам <b>{TRIAL_DAYS} дня бесплатного теста</b>.\n\n"
            "Активируйте и проверьте сами 👇"
        ),
    },
    3: {
        "kind": "discount",
        "text": (
            "<b>Белые списки — не проблема 🌍</b>\n\n"
            "Если интернет работает странно: одни сайты открываются, другие нет, а часть приложений вообще "
            "не подключается — это может быть похоже на ограничения или белые списки.\n\n"
            "С <b>ALTLINK VPN</b> пользоваться интернетом проще: подключились и работаете как обычно.\n\n"
            "Вернитесь со скидкой <b>{DISCOUNT}%</b> по промокоду <code>{PROMO}</code> 👇"
        ),
    },
    4: {
        "kind": "trial",
        "text": (
            "<b>Запасной доступ к интернету уже ждёт 🚀</b>\n\n"
            "Когда связь нестабильная, сайты не грузятся, а нужные сервисы отваливаются в самый неподходящий "
            "момент — нужен нормальный запасной вариант.\n\n"
            "<b>ALTLINK VPN</b> помогает вернуть стабильный доступ к интернету и пользоваться привычными сервисами.\n\n"
            "Активируйте <b>{TRIAL_DAYS} дня бесплатно</b> и проверьте подключение 👇"
        ),
    },
    5: {
        "kind": "discount",
        "text": (
            "<b>Похоже, вам снова может пригодиться VPN 👀</b>\n\n"
            "Интернет всё чаще работает нестабильно: где-то режут трафик, где-то открываются только разрешённые "
            "ресурсы, а часть сервисов просто не отвечает.\n\n"
            "С <b>ALTLINK VPN</b> всё проще: подключили VPN — и продолжаете пользоваться интернетом.\n\n"
            "Для вас промокод <code>{PROMO}</code> на скидку <b>{DISCOUNT}%</b> 👇"
        ),
    },
    6: {
        "kind": "trial",
        "text": (
            "<b>Не ждите, пока интернет снова ляжет 🔥</b>\n\n"
            "Если у вас уже были ситуации, когда нужный сайт или приложение не открывались — лучше держать VPN "
            "под рукой заранее.\n\n"
            "<b>ALTLINK VPN</b> поможет оставаться на связи даже при ограничениях и нестабильной работе сети.\n\n"
            "Забирайте <b>{TRIAL_DAYS} дня теста бесплатно</b> 👇"
        ),
    },
    7: {
        "kind": "discount",
        "text": (
            "<b>Снова онлайн без лишней боли 💙</b>\n\n"
            "Ограничения, белые списки, нестабильные подключения — всё это мешает нормально пользоваться интернетом.\n\n"
            "С <b>ALTLINK VPN</b> можно быстро подключиться и вернуть доступ к привычным сервисам.\n\n"
            "Вернитесь сейчас со скидкой <b>{DISCOUNT}%</b> по промокоду <code>{PROMO}</code> 👇"
        ),
    },
    8: {
        "kind": "discount",
        "text": (
            "<b>ALTLINK VPN — когда интернет должен работать 😎</b>\n\n"
            "Если обычное подключение снова начинает подводить, сайты не открываются или сеть работает только "
            "«куда разрешено» — включайте <b>ALTLINK VPN</b>.\n\n"
            "Быстрое подключение, стабильные серверы и доступ к привычному интернету.\n\n"
            "Используйте промокод <code>{PROMO}</code> на скидку <b>{DISCOUNT}%</b> 👇"
        ),
    },
}

DISCOUNT_PROMO_TEMPLATE_IDS = (1, 3, 5, 7, 8)
RETURN_TRIAL_TEMPLATE_IDS = (2, 4, 6)


def promo_template_kind(template_id: int) -> str:
    template = PROMO_MESSAGE_TEMPLATES.get(template_id) or PROMO_MESSAGE_TEMPLATES[1]
    return template["kind"]


def render_promo_campaign_message(
    template_id: int,
    *,
    promo_code: str | None = None,
    discount_percent: int = 10,
    trial_days: int = 2,
) -> str:
    template = PROMO_MESSAGE_TEMPLATES.get(template_id) or PROMO_MESSAGE_TEMPLATES[1]
    return template["text"].format(
        PROMO=escape(promo_code or ""),
        DISCOUNT=int(discount_percent),
        TRIAL_DAYS=int(trial_days),
    )


def inactive_subscription_promo_message(
    promo_code: str = "ALT10",
    discount_percent: int = 10,
    *,
    template_id: int = 1,
) -> str:
    return render_promo_campaign_message(
        template_id,
        promo_code=promo_code,
        discount_percent=discount_percent,
    )


def return_trial_offer_message(*, template_id: int = 2, trial_days: int = 2) -> str:
    return render_promo_campaign_message(template_id, trial_days=trial_days)


def trial_followup_message(promo_code: str = "ALT10", discount_percent: int = 10) -> str:
    return inactive_subscription_promo_message(promo_code, discount_percent, template_id=8)


def trial_setup_help_message(support_username: str = "@altlink_support") -> str:
    support = support_username.strip() or "@altlink_support"
    if not support.startswith("@"):
        support = f"@{support}"
    return (
        "👋 Видим, что пробный период уже активирован, но подключение пока не появилось.\n\n"
        "Если вы столкнулись с трудностями при настройке, вы всегда можете обратиться в нашу поддержку: "
        f"{support}"
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
