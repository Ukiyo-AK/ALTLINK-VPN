"""Preserve explicit multiple Start server assignments.

Revision ID: 20260922_0020
Revises: 20260914_0019
"""

import sqlalchemy as sa
from alembic import op

revision = "20260922_0020"
down_revision = "20260914_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Runtime schema preparation may already have created the table.
    inspector = sa.inspect(op.get_bind())
    if "user_start_servers" not in inspector.get_table_names():
        op.create_table(
            "user_start_servers",
            sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("server_id", sa.String(), sa.ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True),
        )
        op.create_index("ix_user_start_servers_server_id", "user_start_servers", ["server_id"])
    op.execute(sa.text(
        "INSERT INTO user_start_servers (user_id, server_id) "
        "SELECT users.id, servers.id FROM users JOIN servers ON servers.id = users.assigned_server_id "
        "WHERE servers.server_type = 'ten_gbit' "
        "AND NOT EXISTS (SELECT 1 FROM user_start_servers WHERE user_start_servers.user_id = users.id)"
    ))


def downgrade() -> None:
    op.drop_table("user_start_servers")
