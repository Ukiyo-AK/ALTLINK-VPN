"""add server inbound access type

Revision ID: 20260613_0007
Revises: 20260610_0006
Create Date: 2026-06-13 16:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260613_0007"
down_revision = "20260610_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("server_inbounds"):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("server_inbounds")}
    if "access_type" not in existing_columns:
        with op.batch_alter_table("server_inbounds") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "access_type",
                    sa.String(length=32),
                    nullable=False,
                    server_default="regular",
                )
            )

    op.execute("UPDATE server_inbounds SET access_type = 'regular' WHERE access_type IS NULL")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("server_inbounds"):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("server_inbounds")}
    if "access_type" in existing_columns:
        with op.batch_alter_table("server_inbounds") as batch_op:
            batch_op.drop_column("access_type")
