from __future__ import annotations

import asyncio
from typing import Optional

import typer
from sqlalchemy import select

from altlink.application.services.registry import AppContainer
from altlink.domain.plans import DEFAULT_PLAN_SEEDS
from altlink.infrastructure.db.models import Plan, SystemSetting
from altlink.settings import get_settings

app = typer.Typer(help="ALTLINK CLI")


@app.command("seed-defaults")
def seed_defaults() -> None:
    asyncio.run(_seed_defaults())


async def _seed_defaults() -> None:
    container = AppContainer(get_settings())
    try:
        async with container.hub() as hub:
            for seed in DEFAULT_PLAN_SEEDS:
                plan = await hub.session.scalar(select(Plan).where(Plan.code == seed["code"]))
                if plan is None:
                    plan = Plan(**seed)
                    hub.session.add(plan)

            defaults = {
                "traffic_notification_thresholds": hub.settings.traffic_notification_thresholds,
                "grace_period_days": hub.settings.grace_period_days,
                "low_balance_threshold_rub": hub.settings.low_balance_threshold_rub,
            }
            for key, value in defaults.items():
                setting = await hub.session.scalar(select(SystemSetting).where(SystemSetting.key == key))
                if setting is None:
                    hub.session.add(SystemSetting(key=key, value=value))
        typer.echo("Системные настройки и тарифы подготовлены.")
    finally:
        await container.close()


@app.command("create-admin")
def create_admin(
    username: str = typer.Option(..., help="Логин администратора"),
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True),
    full_name: Optional[str] = typer.Option(None, help="ФИО"),
    telegram_id: Optional[int] = typer.Option(None, help="Telegram ID"),
) -> None:
    asyncio.run(_create_admin(username, password, full_name, telegram_id))


async def _create_admin(
    username: str,
    password: str,
    full_name: str | None,
    telegram_id: int | None,
) -> None:
    container = AppContainer(get_settings())
    try:
        async with container.hub() as hub:
            admin = await hub.accounts.create_admin(
                username=username,
                password=password,
                full_name=full_name,
                telegram_id=telegram_id,
            )
            typer.echo(f"Администратор создан: {admin.username}")
    finally:
        await container.close()


if __name__ == "__main__":
    app()

