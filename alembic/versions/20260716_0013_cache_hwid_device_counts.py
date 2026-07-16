"""cache Remnawave HWID device counts on users

Revision ID: 20260716_0013
Revises: 20260716_0012
Create Date: 2026-07-16 15:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260716_0013"
down_revision = "20260716_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("users"):
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    if "hwid_device_count" not in columns:
        op.add_column(
            "users",
            sa.Column("hwid_device_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if "hwid_devices_checked_at" not in columns:
        op.add_column(
            "users",
            sa.Column("hwid_devices_checked_at", sa.DateTime(timezone=True), nullable=True),
        )

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("users")}
    if "ix_users_hwid_device_count" not in indexes:
        op.create_index("ix_users_hwid_device_count", "users", ["hwid_device_count"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("users"):
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "ix_users_hwid_device_count" in indexes:
        op.drop_index("ix_users_hwid_device_count", table_name="users")
    if "hwid_devices_checked_at" in columns:
        op.drop_column("users", "hwid_devices_checked_at")
    if "hwid_device_count" in columns:
        op.drop_column("users", "hwid_device_count")
