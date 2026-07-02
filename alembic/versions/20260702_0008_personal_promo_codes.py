"""add personal promo code ownership

Revision ID: 20260702_0008
Revises: 20260613_0007
Create Date: 2026-07-02 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260702_0008"
down_revision = "20260613_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("promo_codes"):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("promo_codes")}
    if "assigned_user_id" not in existing_columns:
        with op.batch_alter_table("promo_codes") as batch_op:
            batch_op.add_column(sa.Column("assigned_user_id", sa.String(length=36), nullable=True))

    inspector = sa.inspect(bind)
    existing_indexes = {index["name"] for index in inspector.get_indexes("promo_codes")}
    if "ix_promo_codes_assigned_user_id" not in existing_indexes:
        op.create_index(
            "ix_promo_codes_assigned_user_id",
            "promo_codes",
            ["assigned_user_id"],
            unique=False,
        )

    inspector = sa.inspect(bind)
    has_user_foreign_key = any(
        foreign_key.get("constrained_columns") == ["assigned_user_id"]
        and foreign_key.get("referred_table") == "users"
        for foreign_key in inspector.get_foreign_keys("promo_codes")
    )
    if not has_user_foreign_key:
        with op.batch_alter_table("promo_codes") as batch_op:
            batch_op.create_foreign_key(
                "fk_promo_codes_assigned_user_id_users",
                "users",
                ["assigned_user_id"],
                ["id"],
                ondelete="CASCADE",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("promo_codes"):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("promo_codes")}
    if "assigned_user_id" not in existing_columns:
        return

    with op.batch_alter_table("promo_codes") as batch_op:
        batch_op.drop_column("assigned_user_id")
