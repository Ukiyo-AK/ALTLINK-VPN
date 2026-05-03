"""add registration and consent fields to users

Revision ID: 20260421_0002
Revises: 20260323_0001
Create Date: 2026-04-21 16:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260421_0002"
down_revision = "20260323_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("users"):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("users")}
    with op.batch_alter_table("users") as batch_op:
        if "registration_completed_at" not in existing_columns:
            batch_op.add_column(sa.Column("registration_completed_at", sa.DateTime(timezone=True), nullable=True))
        if "consent_accepted_at" not in existing_columns:
            batch_op.add_column(sa.Column("consent_accepted_at", sa.DateTime(timezone=True), nullable=True))
        if "consent_version" not in existing_columns:
            batch_op.add_column(sa.Column("consent_version", sa.String(length=64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("users"):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("users")}
    with op.batch_alter_table("users") as batch_op:
        if "consent_version" in existing_columns:
            batch_op.drop_column("consent_version")
        if "consent_accepted_at" in existing_columns:
            batch_op.drop_column("consent_accepted_at")
        if "registration_completed_at" in existing_columns:
            batch_op.drop_column("registration_completed_at")
