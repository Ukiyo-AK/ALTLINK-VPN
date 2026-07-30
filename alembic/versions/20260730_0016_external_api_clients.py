"""add external api clients

Revision ID: 20260730_0016
Revises: 20260730_0015
Create Date: 2026-07-30 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260730_0016"
down_revision = "20260730_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "external_api_clients" in set(inspector.get_table_names()):
        return

    op.create_table(
        "external_api_clients",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(length=128), nullable=True),
        sa.Column("request_count", sa.BigInteger(), nullable=False),
        sa.Column("created_by_admin_id", sa.String(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_admin_id"],
            ["admin_users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_prefix"),
    )
    op.create_index(
        op.f("ix_external_api_clients_created_by_admin_id"),
        "external_api_clients",
        ["created_by_admin_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_external_api_clients_expires_at"),
        "external_api_clients",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_external_api_clients_is_active"),
        "external_api_clients",
        ["is_active"],
        unique=False,
    )
    op.create_index(
        op.f("ix_external_api_clients_key_prefix"),
        "external_api_clients",
        ["key_prefix"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "external_api_clients" not in set(inspector.get_table_names()):
        return
    op.drop_table("external_api_clients")
