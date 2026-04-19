from __future__ import annotations

from decimal import Decimal

from altlink.domain.enums import PlanCode

GIGABYTE = 1024**3
WHITELIST_GB_PRICE_RUB = Decimal("4")

DEFAULT_PLAN_SEEDS = [
    {
        "code": PlanCode.TRIAL,
        "name": "Тестовый период",
        "price_rub": Decimal("0"),
        "period_days": 2,
        "traffic_limit_bytes": None,
        "is_trial": True,
        "description": "Бесплатный доступ на 2 дня с автоматическим назначением одного 10 Гбит сервера.",
        "sort_order": 0,
    },
    {
        "code": PlanCode.SINGLE_10GBIT,
        "name": "Один сервер 10 Гбит",
        "price_rub": Decimal("69"),
        "period_days": 30,
        "traffic_limit_bytes": None,
        "is_trial": False,
        "description": (
            "Один автоматически назначенный 10 Гбит сервер. "
            "Серверы типа «Белые списки» доступны отдельно и тарифицируются по 4 ₽ за ГБ."
        ),
        "sort_order": 10,
    },
    {
        "code": PlanCode.UNLIMITED,
        "name": "Безлимит",
        "price_rub": Decimal("200"),
        "period_days": 30,
        "traffic_limit_bytes": None,
        "is_trial": False,
        "description": "Полный доступ ко всем активным серверам без ограничений по трафику.",
        "sort_order": 20,
    },
]
