"""change Start whitelist billing price to two rubles

Revision ID: 20260708_0010
Revises: 20260708_0009
Create Date: 2026-07-08 13:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260708_0010"
down_revision = "20260708_0009"
branch_labels = None
depends_on = None

START_PLAN_CODES = ("single_10gbit", "single_10gbit_weekly")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("plans"):
        return

    bind.execute(
        sa.text(
            """
            UPDATE plans
            SET description = CASE CAST(code AS TEXT)
                WHEN 'single_10gbit' THEN
                    'Один автоматически назначенный 10 Гбит сервер. Серверы типа «Белые списки» доступны отдельно и тарифицируются по 2 ₽ за ГБ. При балансе -50 ₽ доступ к ним временно закрывается. Лимит — 2 устройства.'
                WHEN 'single_10gbit_weekly' THEN
                    'Тот же доступ к 10 Гбит серверу, но с еженедельным списанием. Серверы типа «Белые списки» доступны отдельно и тарифицируются по 2 ₽ за ГБ. При балансе -50 ₽ доступ к ним временно закрывается. Лимит — 2 устройства.'
                ELSE description
            END
            WHERE CAST(code AS TEXT) IN :start_plan_codes
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
