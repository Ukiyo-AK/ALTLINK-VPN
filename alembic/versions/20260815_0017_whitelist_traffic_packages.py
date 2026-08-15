"""add versioned whitelist traffic billing and packages

Revision ID: 20260815_0017
Revises: 20260730_0016
Create Date: 2026-08-15 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260815_0017"
down_revision = "20260730_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    user_columns = {item["name"] for item in inspector.get_columns("users")}
    if "whitelist_extra_traffic_bytes" not in user_columns:
        op.add_column(
            "users",
            sa.Column("whitelist_extra_traffic_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        )

    subscription_columns = {item["name"] for item in inspector.get_columns("subscriptions")}
    subscription_additions = (
        ("whitelist_billing_version", sa.Integer(), "1"),
        ("whitelist_included_limit_bytes", sa.BigInteger(), "0"),
        ("whitelist_included_consumed_bytes", sa.BigInteger(), "0"),
        ("whitelist_usage_cursor_bytes", sa.BigInteger(), "-1"),
        ("whitelist_traffic_accounted_bytes", sa.BigInteger(), "0"),
        ("whitelist_notification_threshold", sa.Integer(), "0"),
    )
    for column_name, column_type, default_value in subscription_additions:
        if column_name not in subscription_columns:
            op.add_column(
                "subscriptions",
                sa.Column(column_name, column_type, nullable=False, server_default=default_value),
            )
    op.execute(
        """
        UPDATE subscriptions
        SET whitelist_included_consumed_bytes = CASE
            WHEN whitelist_traffic_used_bytes < whitelist_included_limit_bytes
                THEN whitelist_traffic_used_bytes
            ELSE whitelist_included_limit_bytes
        END
        WHERE whitelist_billing_version >= 2
          AND whitelist_included_consumed_bytes = 0
          AND whitelist_traffic_used_bytes > 0
        """
    )

    table_names = set(inspector.get_table_names())
    if "whitelist_package_purchases" not in table_names:
        op.create_table(
            "whitelist_package_purchases",
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("subscription_id", sa.String(), nullable=True),
            sa.Column("request_key", sa.String(length=64), nullable=False),
            sa.Column("package_code", sa.String(length=16), nullable=False),
            sa.Column("traffic_bytes", sa.BigInteger(), nullable=False),
            sa.Column("price_rub", sa.Numeric(12, 2), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="completed"),
            sa.Column("balance_transaction_id", sa.String(), nullable=True),
            sa.Column("created_by_admin_id", sa.String(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["balance_transaction_id"], ["balance_transactions.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["created_by_admin_id"], ["admin_users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("request_key", name="uq_whitelist_package_purchase_request"),
        )
        op.create_index("ix_whitelist_package_purchases_user_id", "whitelist_package_purchases", ["user_id"])
        op.create_index(
            "ix_whitelist_package_purchases_subscription_id",
            "whitelist_package_purchases",
            ["subscription_id"],
        )
    else:
        index_names = {item["name"] for item in inspector.get_indexes("whitelist_package_purchases")}
        if "ix_whitelist_package_purchases_user_id" not in index_names:
            op.create_index("ix_whitelist_package_purchases_user_id", "whitelist_package_purchases", ["user_id"])
        if "ix_whitelist_package_purchases_subscription_id" not in index_names:
            op.create_index(
                "ix_whitelist_package_purchases_subscription_id",
                "whitelist_package_purchases",
                ["subscription_id"],
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "whitelist_package_purchases" in set(inspector.get_table_names()):
        op.drop_table("whitelist_package_purchases")
    subscription_columns = {item["name"] for item in inspector.get_columns("subscriptions")}
    for column_name in (
        "whitelist_notification_threshold",
        "whitelist_traffic_accounted_bytes",
        "whitelist_usage_cursor_bytes",
        "whitelist_included_consumed_bytes",
        "whitelist_included_limit_bytes",
        "whitelist_billing_version",
    ):
        if column_name in subscription_columns:
            op.drop_column("subscriptions", column_name)
    user_columns = {item["name"] for item in inspector.get_columns("users")}
    if "whitelist_extra_traffic_bytes" in user_columns:
        op.drop_column("users", "whitelist_extra_traffic_bytes")
