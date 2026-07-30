"""add support message attachments

Revision ID: 20260730_0015
Revises: 20260722_0014
Create Date: 2026-07-30 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260730_0015"
down_revision = "20260722_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "support_messages" not in set(inspector.get_table_names()):
        return

    columns = {column["name"] for column in inspector.get_columns("support_messages")}
    additions = {
        "attachment_path": sa.String(length=255),
        "attachment_mime_type": sa.String(length=64),
        "attachment_original_name": sa.String(length=255),
        "attachment_size": sa.Integer(),
    }
    for name, column_type in additions.items():
        if name not in columns:
            op.add_column("support_messages", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "support_messages" not in set(inspector.get_table_names()):
        return

    columns = {column["name"] for column in inspector.get_columns("support_messages")}
    for name in (
        "attachment_size",
        "attachment_original_name",
        "attachment_mime_type",
        "attachment_path",
    ):
        if name in columns:
            op.drop_column("support_messages", name)
