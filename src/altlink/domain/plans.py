from __future__ import annotations

from decimal import Decimal

from altlink.domain.enums import PlanCode

GIGABYTE = 1024**3
WHITELIST_GB_PRICE_RUB = Decimal("1")
START_WHITELIST_BALANCE_FLOOR_RUB = Decimal("-50.00")

SINGLE_10GBIT_MONTHLY_PRICE_RUB = Decimal("69")
UNLIMITED_MONTHLY_PRICE_RUB = Decimal("199")
# Weekly 10 Gbit is intentionally rounded to a clean customer-facing price.
SINGLE_10GBIT_WEEKLY_PRICE_RUB = Decimal("25")
# Weekly Pro is also pinned to a clean customer-facing price.
UNLIMITED_WEEKLY_PRICE_RUB = Decimal("65")

TEN_GBIT_PLAN_CODES = {PlanCode.SINGLE_10GBIT, PlanCode.SINGLE_10GBIT_WEEKLY}
UNLIMITED_PLAN_CODES = {PlanCode.UNLIMITED, PlanCode.UNLIMITED_WEEKLY}
PAID_PLAN_CODES = TEN_GBIT_PLAN_CODES | UNLIMITED_PLAN_CODES

DEFAULT_PLAN_SEEDS = [
    {
        "code": PlanCode.TRIAL,
        "name": "Тестовый период",
        "price_rub": Decimal("0"),
        "period_days": 2,
        "traffic_limit_bytes": None,
        "device_limit": 8,
        "is_trial": True,
        "description": "Бесплатный доступ на 2 дня с уровнем Pro: все активные серверы и лимит до 8 устройств.",
        "sort_order": 0,
    },
    {
        "code": PlanCode.SINGLE_10GBIT,
        "name": "Start • ежемесячно",
        "price_rub": SINGLE_10GBIT_MONTHLY_PRICE_RUB,
        "period_days": 30,
        "traffic_limit_bytes": None,
        "device_limit": 2,
        "is_trial": False,
        "description": (
            "Один автоматически назначенный 10 Гбит сервер. "
            "Серверы типа «Белые списки» доступны отдельно и тарифицируются по 1 ₽ за ГБ. "
            "При балансе -50 ₽ доступ к ним временно закрывается. "
            "Лимит — 2 устройства."
        ),
        "sort_order": 10,
    },
    {
        "code": PlanCode.SINGLE_10GBIT_WEEKLY,
        "name": "Start • еженедельно",
        "price_rub": SINGLE_10GBIT_WEEKLY_PRICE_RUB,
        "period_days": 7,
        "traffic_limit_bytes": None,
        "device_limit": 2,
        "is_trial": False,
        "description": (
            "Тот же доступ к 10 Гбит серверу, но с еженедельным списанием. "
            "Серверы типа «Белые списки» доступны отдельно и тарифицируются по 1 ₽ за ГБ. "
            "При балансе -50 ₽ доступ к ним временно закрывается. "
            "В пересчёте на месяц стоит на 30% дороже. Лимит — 2 устройства."
        ),
        "sort_order": 15,
    },
    {
        "code": PlanCode.UNLIMITED,
        "name": "Pro • ежемесячно",
        "price_rub": UNLIMITED_MONTHLY_PRICE_RUB,
        "period_days": 30,
        "traffic_limit_bytes": None,
        "device_limit": 8,
        "is_trial": False,
        "description": "Полный доступ ко всем активным серверам. Трафик на них не тарифицируется. Лимит — 8 устройств.",
        "sort_order": 20,
    },
    {
        "code": PlanCode.UNLIMITED_WEEKLY,
        "name": "Pro • еженедельно",
        "price_rub": UNLIMITED_WEEKLY_PRICE_RUB,
        "period_days": 7,
        "traffic_limit_bytes": None,
        "device_limit": 8,
        "is_trial": False,
        "description": "Безлимит на все серверы с еженедельным списанием. В пересчёте на месяц стоит на 30% дороже. Лимит — 8 устройств.",
        "sort_order": 25,
    },
]


def parse_plan_code(raw_value: str | None) -> PlanCode | None:
    if raw_value is None:
        return None
    try:
        return PlanCode(raw_value)
    except ValueError:
        return None


def parse_paid_plan_code(raw_value: str | None) -> PlanCode | None:
    plan_code = parse_plan_code(raw_value)
    if plan_code in {None, PlanCode.TRIAL}:
        return None
    return plan_code if plan_code in PAID_PLAN_CODES else None


def is_metered_plan_code(plan_code: PlanCode | None) -> bool:
    return plan_code in TEN_GBIT_PLAN_CODES


def is_unlimited_plan_code(plan_code: PlanCode | None) -> bool:
    return plan_code in UNLIMITED_PLAN_CODES
