"""add support request messages

Revision ID: 20260610_0006
Revises: 20260423_0005
Create Date: 2026-06-10 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260610_0006"
down_revision = "20260423_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("support_messages"):
        op.create_table(
            "support_messages",
            sa.Column("support_request_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("admin_id", sa.String(length=36), nullable=True),
            sa.Column("sender_type", sa.String(length=16), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["admin_id"], ["admin_users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["support_request_id"], ["support_requests.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = sa.inspect(bind)

    existing_indexes = {index["name"] for index in inspector.get_indexes("support_messages")}
    for index_name, columns in {
        op.f("ix_support_messages_support_request_id"): ["support_request_id"],
        op.f("ix_support_messages_user_id"): ["user_id"],
        op.f("ix_support_messages_admin_id"): ["admin_id"],
    }.items():
        if index_name not in existing_indexes:
            op.create_index(index_name, "support_messages", columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("support_messages"):
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes("support_messages")}
    for index_name in (
        op.f("ix_support_messages_admin_id"),
        op.f("ix_support_messages_user_id"),
        op.f("ix_support_messages_support_request_id"),
    ):
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="support_messages")
    op.drop_table("support_messages")
