"""change Start whitelist billing price and forgive legacy debt

Revision ID: 20260708_0009
Revises: 20260702_0008
Create Date: 2026-07-08 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260708_0009"
down_revision = "20260702_0008"
branch_labels = None
depends_on = None

START_PLAN_CODES = ("single_10gbit", "single_10gbit_weekly")


def _has_tables(inspector: sa.Inspector, *names: str) -> bool:
    return all(inspector.has_table(name) for name in names)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_tables(inspector, "plans"):
        bind.execute(
            sa.text(
                """
                UPDATE plans
                SET description = CASE CAST(code AS TEXT)
                    WHEN 'single_10gbit' THEN
                        'Один автоматически назначенный 10 Гбит сервер. Серверы типа «Белые списки» доступны отдельно и тарифицируются по 1 ₽ за ГБ. При балансе -50 ₽ доступ к ним временно закрывается. Лимит — 2 устройства.'
                    WHEN 'single_10gbit_weekly' THEN
                        'Тот же доступ к 10 Гбит серверу, но с еженедельным списанием. Серверы типа «Белые списки» доступны отдельно и тарифицируются по 1 ₽ за ГБ. При балансе -50 ₽ доступ к ним временно закрывается. Лимит — 2 устройства.'
                    ELSE description
                END
                WHERE CAST(code AS TEXT) IN :start_plan_codes
                """
            ).bindparams(sa.bindparam("start_plan_codes", expanding=True)),
            {"start_plan_codes": START_PLAN_CODES},
        )

    if not _has_tables(inspector, "users", "subscriptions", "plans"):
        return

    bind.execute(
        sa.text(
            """
            UPDATE users
            SET balance_rub = 0
            WHERE balance_rub < 0
              AND EXISTS (
                SELECT 1
                FROM subscriptions
                JOIN plans ON plans.id = subscriptions.plan_id
                WHERE subscriptions.user_id = users.id
                  AND CAST(plans.code AS TEXT) IN :start_plan_codes
              )
            """
        ).bindparams(sa.bindparam("start_plan_codes", expanding=True)),
        {"start_plan_codes": START_PLAN_CODES},
    )

    bind.execute(
        sa.text(
            """
            UPDATE subscriptions
            SET accrued_debt_rub = 0,
                whitelist_traffic_billed_bytes = GREATEST(
                    whitelist_traffic_billed_bytes,
                    whitelist_traffic_used_bytes
                )
            FROM plans
            WHERE plans.id = subscriptions.plan_id
              AND CAST(plans.code AS TEXT) IN :start_plan_codes
            """
        ).bindparams(sa.bindparam("start_plan_codes", expanding=True)),
        {"start_plan_codes": START_PLAN_CODES},
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("plans"):
        return

    bind.execute(
        sa.text(
            """
            UPDATE plans
            SET description = REPLACE(description, '1 ₽ за ГБ', '4 ₽ за ГБ')
            WHERE description IS NOT NULL
              AND CAST(code AS TEXT) IN :start_plan_codes
            """
        ).bindparams(sa.bindparam("start_plan_codes", expanding=True)),
        {"start_plan_codes": START_PLAN_CODES},
    )
