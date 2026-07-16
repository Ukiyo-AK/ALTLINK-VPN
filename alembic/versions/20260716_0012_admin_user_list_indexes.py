"""add indexes for admin user list filters

Revision ID: 20260716_0012
Revises: 20260708_0011
Create Date: 2026-07-16 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260716_0012"
down_revision = "20260708_0011"
branch_labels = None
depends_on = None


INDEXES = (
    ("users", "ix_users_created_at", ("created_at",)),
    ("users", "ix_users_status", ("status",)),
    ("users", "ix_users_balance_rub", ("balance_rub",)),
    ("users", "ix_users_last_seen_at", ("last_seen_at",)),
    ("subscriptions", "ix_subscriptions_user_created_at", ("user_id", "created_at")),
    ("subscriptions", "ix_subscriptions_next_billing_at", ("next_billing_at",)),
    ("traffic_snapshots", "ix_traffic_snapshots_user_lifetime", ("user_id", "lifetime_used_bytes")),
    ("traffic_snapshots", "ix_traffic_snapshots_server_user_lifetime", ("server_id", "user_id", "lifetime_used_bytes")),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    existing_indexes_by_table = {
        table: {index["name"] for index in inspector.get_indexes(table)}
        for table, _, _ in INDEXES
        if table in existing_tables
    }

    for table, index_name, columns in INDEXES:
        if table not in existing_tables:
            continue
        if index_name in existing_indexes_by_table.get(table, set()):
            continue
        op.create_index(index_name, table, list(columns), unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for table, index_name, _ in reversed(INDEXES):
        if table not in existing_tables:
            continue
        existing_indexes = {index["name"] for index in inspector.get_indexes(table)}
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name=table)
