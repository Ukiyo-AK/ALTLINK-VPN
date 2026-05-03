"""add promo codes and referral fields

Revision ID: 20260423_0005
Revises: 20260421_0004
Create Date: 2026-04-23 10:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260423_0005"
down_revision = "20260421_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("users"):
        existing_columns = {column["name"] for column in inspector.get_columns("users")}
        existing_indexes = {index["name"] for index in inspector.get_indexes("users")}
        existing_foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("users")}

        with op.batch_alter_table("users") as batch_op:
            if "referral_code" not in existing_columns:
                batch_op.add_column(sa.Column("referral_code", sa.String(length=32), nullable=True))
            if "referred_by_user_id" not in existing_columns:
                batch_op.add_column(sa.Column("referred_by_user_id", sa.String(length=36), nullable=True))
            if "referral_reward_granted_at" not in existing_columns:
                batch_op.add_column(sa.Column("referral_reward_granted_at", sa.DateTime(timezone=True), nullable=True))
            if "ix_users_referred_by_user_id" not in existing_indexes:
                batch_op.create_index("ix_users_referred_by_user_id", ["referred_by_user_id"], unique=False)
            if "fk_users_referred_by_user_id_users" not in existing_foreign_keys:
                batch_op.create_foreign_key(
                    "fk_users_referred_by_user_id_users",
                    "users",
                    ["referred_by_user_id"],
                    ["id"],
                    ondelete="SET NULL",
                )

    if not inspector.has_table("promo_codes"):
        op.create_table(
            "promo_codes",
            sa.Column("code", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("reward_kind", sa.String(length=32), nullable=False),
            sa.Column("reward_value", sa.Numeric(12, 2), nullable=False),
            sa.Column("usage_limit", sa.Integer(), nullable=True),
            sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("new_users_only", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_by_admin_id", sa.String(length=36), nullable=True),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["created_by_admin_id"], ["admin_users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("code"),
        )
        inspector = sa.inspect(bind)
    promo_indexes = {index["name"] for index in inspector.get_indexes("promo_codes")} if inspector.has_table("promo_codes") else set()
    if "ix_promo_codes_code" not in promo_indexes:
        op.create_index("ix_promo_codes_code", "promo_codes", ["code"], unique=True)

    if not inspector.has_table("promo_code_redemptions"):
        op.create_table(
            "promo_code_redemptions",
            sa.Column("promo_code_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("applied_subscription_id", sa.String(length=36), nullable=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reward_value_applied", sa.Numeric(12, 2), nullable=True),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["applied_subscription_id"], ["subscriptions.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["promo_code_id"], ["promo_codes.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("promo_code_id", "user_id", name="uq_promo_code_redemption_user"),
        )
        inspector = sa.inspect(bind)
    redemption_indexes = (
        {index["name"] for index in inspector.get_indexes("promo_code_redemptions")}
        if inspector.has_table("promo_code_redemptions")
        else set()
    )
    if "ix_promo_code_redemptions_promo_code_id" not in redemption_indexes:
        op.create_index("ix_promo_code_redemptions_promo_code_id", "promo_code_redemptions", ["promo_code_id"], unique=False)
    if "ix_promo_code_redemptions_user_id" not in redemption_indexes:
        op.create_index("ix_promo_code_redemptions_user_id", "promo_code_redemptions", ["user_id"], unique=False)
    if "ix_promo_code_redemptions_applied_subscription_id" not in redemption_indexes:
        op.create_index(
            "ix_promo_code_redemptions_applied_subscription_id",
            "promo_code_redemptions",
            ["applied_subscription_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("promo_code_redemptions"):
        redemption_indexes = {index["name"] for index in inspector.get_indexes("promo_code_redemptions")}
        if "ix_promo_code_redemptions_applied_subscription_id" in redemption_indexes:
            op.drop_index("ix_promo_code_redemptions_applied_subscription_id", table_name="promo_code_redemptions")
        if "ix_promo_code_redemptions_user_id" in redemption_indexes:
            op.drop_index("ix_promo_code_redemptions_user_id", table_name="promo_code_redemptions")
        if "ix_promo_code_redemptions_promo_code_id" in redemption_indexes:
            op.drop_index("ix_promo_code_redemptions_promo_code_id", table_name="promo_code_redemptions")
        op.drop_table("promo_code_redemptions")

    if inspector.has_table("promo_codes"):
        promo_indexes = {index["name"] for index in inspector.get_indexes("promo_codes")}
        if "ix_promo_codes_code" in promo_indexes:
            op.drop_index("ix_promo_codes_code", table_name="promo_codes")
        op.drop_table("promo_codes")

    if inspector.has_table("users"):
        existing_columns = {column["name"] for column in inspector.get_columns("users")}
        existing_indexes = {index["name"] for index in inspector.get_indexes("users")}
        existing_foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("users")}
        with op.batch_alter_table("users") as batch_op:
            if "fk_users_referred_by_user_id_users" in existing_foreign_keys:
                batch_op.drop_constraint("fk_users_referred_by_user_id_users", type_="foreignkey")
            if "ix_users_referred_by_user_id" in existing_indexes:
                batch_op.drop_index("ix_users_referred_by_user_id")
            if "referral_reward_granted_at" in existing_columns:
                batch_op.drop_column("referral_reward_granted_at")
            if "referred_by_user_id" in existing_columns:
                batch_op.drop_column("referred_by_user_id")
            if "referral_code" in existing_columns:
                batch_op.drop_column("referral_code")
