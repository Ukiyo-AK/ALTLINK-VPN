from __future__ import annotations

from decimal import Decimal

GIGABYTE = 1024**3
UNLIMITED_PLAN_CODE = "unlimited_30d"
LIMITED_PLAN_CODE = "limited_50gb_30d"
TRIAL_PLAN_CODE = "trial_2d"

DEFAULT_PLANS = [
    {
        "code": TRIAL_PLAN_CODE,
        "name_ru": "Тестовый период",
        "kind": "trial",
        "price_rub": Decimal("0.00"),
        "duration_days": 2,
        "traffic_limit_bytes": None,
        "sort_order": 1,
        "is_trial": True,
    },
    {
        "code": UNLIMITED_PLAN_CODE,
        "name_ru": "Безлимит на 30 дней",
        "kind": "unlimited",
        "price_rub": Decimal("200.00"),
        "duration_days": 30,
        "traffic_limit_bytes": None,
        "sort_order": 2,
        "is_trial": False,
    },
    {
        "code": LIMITED_PLAN_CODE,
        "name_ru": "50 ГБ на 30 дней",
        "kind": "limited",
        "price_rub": Decimal("100.00"),
        "duration_days": 30,
        "traffic_limit_bytes": 50 * GIGABYTE,
        "sort_order": 3,
        "is_trial": False,
    },
]

