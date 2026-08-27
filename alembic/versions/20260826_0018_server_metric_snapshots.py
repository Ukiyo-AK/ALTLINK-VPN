"""add server metric snapshots

Revision ID: 20260826_0018
Revises: 20260815_0017
Create Date: 2026-08-26 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260826_0018"
down_revision = "20260815_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "server_metric_snapshots" in set(inspector.get_table_names()):
        return
    op.create_table(
        "server_metric_snapshots",
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("remnawave_node_uuid", sa.String(length=64), nullable=False),
        sa.Column("server_name", sa.String(length=255), nullable=False),
        sa.Column("country_code", sa.String(length=8), nullable=True),
        sa.Column("server_type", sa.String(length=32), nullable=False),
        sa.Column("is_operational", sa.Boolean(), nullable=False),
        sa.Column("is_connected", sa.Boolean(), nullable=False),
        sa.Column("is_available", sa.Boolean(), nullable=False),
        sa.Column("assigned_users", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("online_users", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("xray_uptime", sa.String(length=64), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_server_metric_snapshots_server_captured",
        "server_metric_snapshots",
        ["server_id", "captured_at"],
    )
    op.create_index(
        "ix_server_metric_snapshots_captured_at",
        "server_metric_snapshots",
        ["captured_at"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "server_metric_snapshots" in set(inspector.get_table_names()):
        op.drop_table("server_metric_snapshots")
