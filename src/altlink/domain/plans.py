from __future__ import annotations

from decimal import Decimal

from altlink.domain.enums import PlanCode

GIGABYTE = 1024**3

DEFAULT_PLAN_SEEDS = [
    {
        "code": PlanCode.TRIAL,
        "name": "Тестовый период",
        "price_rub": Decimal("0"),
        "period_days": 2,
        "traffic_limit_bytes": None,
        "is_trial": True,
        "description": "Бесплатный тестовый доступ на 2 дня.",
        "sort_order": 0,
    },
    {
        "code": PlanCode.UNLIMITED,
        "name": "Безлимит",
        "price_rub": Decimal("200"),
        "period_days": 30,
        "traffic_limit_bytes": None,
        "is_trial": False,
        "description": "Полный доступ ко всем активным серверам без лимита трафика.",
        "sort_order": 10,
    },
    {
        "code": PlanCode.LIMITED_50GB,
        "name": "Лимитный 50 ГБ",
        "price_rub": Decimal("100"),
        "period_days": 30,
        "traffic_limit_bytes": 50 * GIGABYTE,
        "is_trial": False,
        "description": "Доступ ко всем активным серверам с лимитом 50 ГБ на 30 дней.",
        "sort_order": 20,
    },
]

