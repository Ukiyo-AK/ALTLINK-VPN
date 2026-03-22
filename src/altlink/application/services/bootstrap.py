from __future__ import annotations

from sqlalchemy import select

from altlink.application.services.base import ServiceBase
from altlink.domain.constants import DEFAULT_PLANS
from altlink.domain.enums import EventLevel, PlanKind
from altlink.infrastructure.db.models import Plan, SystemSetting


class BootstrapService(ServiceBase):
    async def ensure_defaults(self) -> None:
        existing = {plan.code: plan for plan in (await self.session.execute(select(Plan))).scalars()}
        for plan_data in DEFAULT_PLANS:
            plan = existing.get(plan_data["code"])
            if plan is None:
                plan = Plan(
                    code=plan_data["code"],
                    name_ru=plan_data["name_ru"],
                    kind=PlanKind(plan_data["kind"]),
                    price_rub=plan_data["price_rub"],
                    duration_days=plan_data["duration_days"],
                    traffic_limit_bytes=plan_data["traffic_limit_bytes"],
                    sort_order=plan_data["sort_order"],
                    is_trial=plan_data["is_trial"],
                    is_active=True,
                )
                self.session.add(plan)
            else:
                plan.name_ru = plan_data["name_ru"]
                plan.kind = PlanKind(plan_data["kind"])
                plan.price_rub = plan_data["price_rub"]
                plan.duration_days = plan_data["duration_days"]
                plan.traffic_limit_bytes = plan_data["traffic_limit_bytes"]
                plan.sort_order = plan_data["sort_order"]
                plan.is_trial = plan_data["is_trial"]
                plan.is_active = True

        defaults = {
            "traffic_notify_thresholds": {
                "value": self.settings.traffic_notify_thresholds,
                "description": "Пороговые значения уведомлений по трафику.",
            },
            "low_balance_threshold_rub": {
                "value": self.settings.low_balance_threshold_rub,
                "description": "Нижний порог баланса для уведомлений.",
            },
            "low_balance_notify_days": {
                "value": self.settings.low_balance_notify_days,
                "description": "За сколько дней предупреждать о скором списании.",
            },
            "trial_duration_days": {
                "value": self.settings.trial_duration_days,
                "description": "Длительность тестового периода.",
            },
            "grace_period_days": {
                "value": self.settings.grace_period_days,
                "description": "Длительность grace period.",
            },
            "grace_speed_limit_supported": {
                "value": False,
                "description": "Официальный API Remnawave не предоставляет лимит скорости пользователя.",
            },
        }
        for key, metadata in defaults.items():
            setting = await self.session.get(SystemSetting, key)
            if setting is None:
                self.session.add(
                    SystemSetting(
                        key=key,
                        value=metadata["value"],
                        description=metadata["description"],
                    )
                )
            else:
                setting.value = metadata["value"]
                setting.description = metadata["description"]

        await self.log_event(
            scope="bootstrap",
            level=EventLevel.INFO,
            title="Системные настройки и тарифы проверены",
        )

