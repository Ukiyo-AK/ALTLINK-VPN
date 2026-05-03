"""add channel verification field to users

Revision ID: 20260421_0004
Revises: 20260421_0003
Create Date: 2026-04-21 18:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260421_0004"
down_revision = "20260421_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("users"):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("users")}
    if "channel_verified_at" in existing_columns:
        return

    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("channel_verified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("users"):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("users")}
    if "channel_verified_at" not in existing_columns:
        return

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("channel_verified_at")
