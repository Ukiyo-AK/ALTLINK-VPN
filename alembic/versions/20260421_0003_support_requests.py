"""add support requests table

Revision ID: 20260421_0003
Revises: 20260421_0002
Create Date: 2026-04-21 17:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260421_0003"
down_revision = "20260421_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("support_requests"):
        op.create_table(
            "support_requests",
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("resolved_by_admin_id", sa.String(), nullable=True),
            sa.Column("status", sa.Enum("new", "resolved", name="supportrequeststatus"), nullable=False),
            sa.Column("topic", sa.String(length=64), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("resolution_comment", sa.Text(), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["resolved_by_admin_id"], ["admin_users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = sa.inspect(bind)

    existing_indexes = {index["name"] for index in inspector.get_indexes("support_requests")}
    resolved_by_index = op.f("ix_support_requests_resolved_by_admin_id")
    user_index = op.f("ix_support_requests_user_id")

    if resolved_by_index not in existing_indexes:
        op.create_index(resolved_by_index, "support_requests", ["resolved_by_admin_id"])
    if user_index not in existing_indexes:
        op.create_index(user_index, "support_requests", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("support_requests"):
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes("support_requests")}
    user_index = op.f("ix_support_requests_user_id")
    resolved_by_index = op.f("ix_support_requests_resolved_by_admin_id")

    if user_index in existing_indexes:
        op.drop_index(user_index, table_name="support_requests")
    if resolved_by_index in existing_indexes:
        op.drop_index(resolved_by_index, table_name="support_requests")
    op.drop_table("support_requests")
