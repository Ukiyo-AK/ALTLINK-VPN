"""initial schema

Revision ID: 20260323_0001
Revises:
Create Date: 2026-03-23 02:20:00.000000
"""

from __future__ import annotations

from alembic import op

from altlink.infrastructure.db.models import Base

revision = "20260323_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
