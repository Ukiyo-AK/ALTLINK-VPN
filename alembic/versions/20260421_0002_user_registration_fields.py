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
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("registration_completed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("consent_accepted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("consent_version", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("consent_version")
        batch_op.drop_column("consent_accepted_at")
        batch_op.drop_column("registration_completed_at")
