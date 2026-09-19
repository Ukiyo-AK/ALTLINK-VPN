"""Enable renewal for existing paid subscriptions; retire old user-facing copy.

Revision ID: 20260914_0019
Revises: 20260826_0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260914_0019"
down_revision = "20260826_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    plans = sa.table("plans", sa.column("id", sa.String), sa.column("is_trial", sa.Boolean))
    subscriptions = sa.table(
        "subscriptions",
        sa.column("plan_id", sa.String),
        sa.column("auto_renew", sa.Boolean),
        sa.column("notes", sa.Text),
    )
    # Only the renewal preference changes. Trials, dates, balances and access stay intact.
    bind.execute(
        subscriptions.update()
        .where(
            subscriptions.c.auto_renew.is_(False),
            subscriptions.c.plan_id.in_(sa.select(plans.c.id).where(plans.c.is_trial.is_(False))),
        )
        .values(auto_renew=True)
    )
    bind.execute(
        subscriptions.update()
        .where(subscriptions.c.notes.contains("Автопродление с промокодом"))
        .values(notes=sa.func.replace(subscriptions.c.notes, "Автопродление с промокодом", "Продление с промокодом"))
    )

    notifications = sa.table(
        "notifications",
        sa.column("status", sa.String),
        sa.column("type", sa.String),
        sa.column("message", sa.Text),
        sa.column("payload", sa.JSON),
        sa.column("dedupe_key", sa.String),
        sa.column("failure_reason", sa.Text),
    )
    pending = notifications.c.status == "pending"
    cta = notifications.c.payload["cta"].as_string()
    # Do not deliver reminders claiming renewal is disabled after enabling it.
    bind.execute(
        notifications.update()
        .where(
            pending,
            sa.or_(cta == "renewal_disabled_expiring", notifications.c.dedupe_key.startswith("renewal-disabled-expiring:")),
        )
        .values(status="failed", failure_reason="Superseded by renewal policy migration 20260914_0019")
    )
    replacements = (
        (notifications.c.type == "low_balance", "Не хватает средств для автопродления.", "Не хватает средств для продления подписки."),
        (cta == "subscription_ended_auto_renew_disabled", "Автопродление было отключено, поэтому доступ остановлен.", "Доступ остановлен."),
        (cta == "subscription_ended_auto_renew_disabled", "Нажмите «Включить автопродление», чтобы снова активировать этот тариф.", "Нажмите «Выбрать тариф», чтобы вернуть доступ."),
        (cta == "topup_enable_auto_renew", "\n\nАвтопродление подписки отключено. Включите его, чтобы доступ продолжился после окончания тарифа.", ""),
        (cta == "topup_resume_subscription", "Пополнение не возобновляет тариф", "Тариф"),
        (cta == "topup_resume_subscription", " автоматически, потому что автопродление было отключено. Возобновите тариф, чтобы вернуть доступ.", " сейчас не активен. Выберите тариф, чтобы вернуть доступ."),
    )
    for condition, old, new in replacements:
        bind.execute(
            notifications.update()
            .where(pending, condition, notifications.c.message.contains(old))
            .values(message=sa.func.replace(notifications.c.message, old, new))
        )


def downgrade() -> None:
    # The original per-subscription choices cannot be reconstructed safely.
    # Never blanket-disable renewal or revive superseded notifications on downgrade.
    pass
