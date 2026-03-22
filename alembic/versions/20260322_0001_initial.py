"""initial schema

Revision ID: 20260322_0001
Revises:
Create Date: 2026-03-22 23:59:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260322_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("is_superuser", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_users")),
        sa.UniqueConstraint("telegram_id", name=op.f("uq_admin_users_telegram_id")),
        sa.UniqueConstraint("username", name=op.f("uq_admin_users_username")),
    )
    op.create_index(op.f("ix_admin_users_telegram_id"), "admin_users", ["telegram_id"], unique=False)
    op.create_index(op.f("ix_admin_users_username"), "admin_users", ["username"], unique=False)

    op.create_table(
        "plans",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name_ru", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.Enum("trial", "unlimited", "limited", name="plankind", native_enum=False, length=32), nullable=False),
        sa.Column("price_rub", sa.Numeric(12, 2), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("traffic_limit_bytes", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("is_trial", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="100", nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plans")),
        sa.UniqueConstraint("code", name=op.f("uq_plans_code")),
    )
    op.create_index(op.f("ix_plans_code"), "plans", ["code"], unique=False)
    op.create_index(op.f("ix_plans_kind"), "plans", ["kind"], unique=False)

    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_system_settings")),
    )

    op.create_table(
        "users",
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_username", sa.String(length=255), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("language_code", sa.String(length=16), nullable=True),
        sa.Column("status", sa.Enum("new", "trial", "active", "grace", "blocked", "canceled", name="userstatus", native_enum=False, length=32), server_default="new", nullable=False),
        sa.Column("balance_rub", sa.Numeric(12, 2), server_default="0.00", nullable=False),
        sa.Column("is_manual_blocked", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("remnawave_user_uuid", sa.String(length=36), nullable=True),
        sa.Column("remnawave_username", sa.String(length=64), nullable=True),
        sa.Column("remnawave_short_uuid", sa.String(length=64), nullable=True),
        sa.Column("remnawave_subscription_url", sa.Text(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_bot_interaction_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_remnawave_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("telegram_id", name=op.f("uq_users_telegram_id")),
        sa.UniqueConstraint("remnawave_user_uuid", name=op.f("uq_users_remnawave_user_uuid")),
        sa.UniqueConstraint("remnawave_username", name=op.f("uq_users_remnawave_username")),
        sa.UniqueConstraint("remnawave_short_uuid", name=op.f("uq_users_remnawave_short_uuid")),
    )
    op.create_index(op.f("ix_users_telegram_id"), "users", ["telegram_id"], unique=False)
    op.create_index(op.f("ix_users_telegram_username"), "users", ["telegram_username"], unique=False)
    op.create_index(op.f("ix_users_status"), "users", ["status"], unique=False)

    op.create_table(
        "servers",
        sa.Column("remnawave_node_uuid", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("country_code", sa.String(length=8), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("active_config_profile_uuid", sa.String(length=36), nullable=True),
        sa.Column("is_managed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_online", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_connected", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_disabled_remote", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("users_online", sa.Integer(), nullable=True),
        sa.Column("current_clients_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_clients_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("load_percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("last_status_message", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_metrics_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_servers")),
        sa.UniqueConstraint("remnawave_node_uuid", name=op.f("uq_servers_remnawave_node_uuid")),
    )
    op.create_index(op.f("ix_servers_remnawave_node_uuid"), "servers", ["remnawave_node_uuid"], unique=False)
    op.create_index(op.f("ix_servers_name"), "servers", ["name"], unique=False)

    op.create_table(
        "topup_requests",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("amount_rub", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.Enum("new", "approved", "rejected", "canceled", name="topuprequeststatus", native_enum=False, length=32), server_default="new", nullable=False),
        sa.Column("user_comment", sa.Text(), nullable=True),
        sa.Column("admin_comment", sa.Text(), nullable=True),
        sa.Column("approved_by_admin_id", sa.String(length=36), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["approved_by_admin_id"], ["admin_users.id"], name=op.f("fk_topup_requests_approved_by_admin_id_admin_users"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_topup_requests_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_topup_requests")),
    )
    op.create_index(op.f("ix_topup_requests_user_id"), "topup_requests", ["user_id"], unique=False)
    op.create_index(op.f("ix_topup_requests_status"), "topup_requests", ["status"], unique=False)
    op.create_index(op.f("ix_topup_requests_approved_by_admin_id"), "topup_requests", ["approved_by_admin_id"], unique=False)

    op.create_table(
        "subscriptions",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.Enum("pending", "trial", "active", "grace", "blocked", "canceled", "expired", name="subscriptionstatus", native_enum=False, length=32), server_default="pending", nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("is_trial", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_billing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grace_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grace_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_traffic_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("renewal_price_rub", sa.Numeric(12, 2), nullable=False),
        sa.Column("debt_rub", sa.Numeric(12, 2), nullable=False),
        sa.Column("traffic_limit_bytes_snapshot", sa.Integer(), nullable=True),
        sa.Column("traffic_used_bytes_cache", sa.Integer(), server_default="0", nullable=False),
        sa.Column("grace_speed_limit_mbps", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], name=op.f("fk_subscriptions_plan_id_plans")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_subscriptions_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
    )
    op.create_index(op.f("ix_subscriptions_user_id"), "subscriptions", ["user_id"], unique=False)
    op.create_index(op.f("ix_subscriptions_plan_id"), "subscriptions", ["plan_id"], unique=False)
    op.create_index(op.f("ix_subscriptions_status"), "subscriptions", ["status"], unique=False)
    op.create_index(op.f("ix_subscriptions_is_current"), "subscriptions", ["is_current"], unique=False)
    op.create_index(op.f("ix_subscriptions_is_trial"), "subscriptions", ["is_trial"], unique=False)
    op.create_index(op.f("ix_subscriptions_next_billing_at"), "subscriptions", ["next_billing_at"], unique=False)
    op.create_index(op.f("ix_subscriptions_grace_ends_at"), "subscriptions", ["grace_ends_at"], unique=False)

    op.create_table(
        "server_inbounds",
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("remnawave_inbound_uuid", sa.String(length=36), nullable=False),
        sa.Column("config_profile_uuid", sa.String(length=36), nullable=True),
        sa.Column("config_profile_inbound_uuid", sa.String(length=36), nullable=True),
        sa.Column("tag", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("network", sa.String(length=64), nullable=True),
        sa.Column("security", sa.String(length=64), nullable=True),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("active_squads", sa.JSON(), nullable=True),
        sa.Column("current_clients_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("raw_inbound", sa.JSON(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], name=op.f("fk_server_inbounds_server_id_servers"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_server_inbounds")),
        sa.UniqueConstraint("server_id", "remnawave_inbound_uuid", name=op.f("uq_server_inbounds_server_id")),
    )
    op.create_index(op.f("ix_server_inbounds_server_id"), "server_inbounds", ["server_id"], unique=False)
    op.create_index(op.f("ix_server_inbounds_remnawave_inbound_uuid"), "server_inbounds", ["remnawave_inbound_uuid"], unique=False)

    op.create_table(
        "user_server_access",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.Enum("active", "blocked", "removed", "unavailable", name="userserveraccessstatus", native_enum=False, length=32), server_default="active", nullable=False),
        sa.Column("config_hint", sa.String(length=255), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], name=op.f("fk_user_server_access_server_id_servers"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_user_server_access_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_server_access")),
        sa.UniqueConstraint("user_id", "server_id", name=op.f("uq_user_server_access_user_id")),
    )
    op.create_index(op.f("ix_user_server_access_user_id"), "user_server_access", ["user_id"], unique=False)
    op.create_index(op.f("ix_user_server_access_server_id"), "user_server_access", ["server_id"], unique=False)
    op.create_index(op.f("ix_user_server_access_status"), "user_server_access", ["status"], unique=False)

    op.create_table(
        "balance_transactions",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("subscription_id", sa.String(length=36), nullable=True),
        sa.Column("topup_request_id", sa.String(length=36), nullable=True),
        sa.Column("admin_user_id", sa.String(length=36), nullable=True),
        sa.Column("transaction_type", sa.Enum("topup", "renewal", "manual_adjustment", "trial_activation", "debt_settlement", "refund", name="balancetransactiontype", native_enum=False, length=32), nullable=False),
        sa.Column("amount_rub", sa.Numeric(12, 2), nullable=False),
        sa.Column("balance_before", sa.Numeric(12, 2), nullable=False),
        sa.Column("balance_after", sa.Numeric(12, 2), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["admin_user_id"], ["admin_users.id"], name=op.f("fk_balance_transactions_admin_user_id_admin_users"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], name=op.f("fk_balance_transactions_subscription_id_subscriptions"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["topup_request_id"], ["topup_requests.id"], name=op.f("fk_balance_transactions_topup_request_id_topup_requests"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_balance_transactions_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_balance_transactions")),
    )
    op.create_index(op.f("ix_balance_transactions_user_id"), "balance_transactions", ["user_id"], unique=False)
    op.create_index(op.f("ix_balance_transactions_subscription_id"), "balance_transactions", ["subscription_id"], unique=False)
    op.create_index(op.f("ix_balance_transactions_topup_request_id"), "balance_transactions", ["topup_request_id"], unique=False)
    op.create_index(op.f("ix_balance_transactions_admin_user_id"), "balance_transactions", ["admin_user_id"], unique=False)

    op.create_table(
        "notifications",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("notification_type", sa.Enum("low_balance", "upcoming_renewal", "grace_started", "grace_reminder", "access_blocked", "topup_approved", "topup_rejected", "traffic_warning", "traffic_limit_reached", "trial_ending", "trial_ended", name="notificationtype", native_enum=False, length=40), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.Enum("pending", "sent", "failed", "canceled", name="notificationstatus", native_enum=False, length=32), server_default="pending", nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=255), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_notifications_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_notifications_dedupe_key")),
    )
    op.create_index(op.f("ix_notifications_user_id"), "notifications", ["user_id"], unique=False)
    op.create_index(op.f("ix_notifications_notification_type"), "notifications", ["notification_type"], unique=False)
    op.create_index(op.f("ix_notifications_status"), "notifications", ["status"], unique=False)
    op.create_index(op.f("ix_notifications_scheduled_for"), "notifications", ["scheduled_for"], unique=False)

    op.create_table(
        "trial_periods",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("subscription_id", sa.String(length=36), nullable=True),
        sa.Column("activated_by_admin_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.Enum("active", "completed", "expired", "canceled", name="trialstatus", native_enum=False, length=32), server_default="active", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["activated_by_admin_id"], ["admin_users.id"], name=op.f("fk_trial_periods_activated_by_admin_id_admin_users"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], name=op.f("fk_trial_periods_subscription_id_subscriptions"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_trial_periods_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trial_periods")),
        sa.UniqueConstraint("user_id", name=op.f("uq_trial_periods_user_id")),
    )
    op.create_index(op.f("ix_trial_periods_user_id"), "trial_periods", ["user_id"], unique=False)
    op.create_index(op.f("ix_trial_periods_status"), "trial_periods", ["status"], unique=False)
    op.create_index(op.f("ix_trial_periods_ends_at"), "trial_periods", ["ends_at"], unique=False)

    op.create_table(
        "traffic_snapshots",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("subscription_id", sa.String(length=36), nullable=True),
        sa.Column("server_id", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lifetime_used_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("node_breakdown", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], name=op.f("fk_traffic_snapshots_server_id_servers"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], name=op.f("fk_traffic_snapshots_subscription_id_subscriptions"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_traffic_snapshots_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_traffic_snapshots")),
    )
    op.create_index(op.f("ix_traffic_snapshots_user_id"), "traffic_snapshots", ["user_id"], unique=False)
    op.create_index(op.f("ix_traffic_snapshots_subscription_id"), "traffic_snapshots", ["subscription_id"], unique=False)
    op.create_index(op.f("ix_traffic_snapshots_server_id"), "traffic_snapshots", ["server_id"], unique=False)
    op.create_index(op.f("ix_traffic_snapshots_snapshot_at"), "traffic_snapshots", ["snapshot_at"], unique=False)

    op.create_table(
        "online_sessions_cache",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("server_id", sa.String(length=36), nullable=True),
        sa.Column("remnawave_node_uuid", sa.String(length=36), nullable=True),
        sa.Column("request_ip", sa.String(length=128), nullable=True),
        sa.Column("user_agent", sa.String(length=1024), nullable=True),
        sa.Column("device_hint", sa.String(length=255), nullable=True),
        sa.Column("inbound_tag", sa.String(length=255), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_online", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], name=op.f("fk_online_sessions_cache_server_id_servers"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_online_sessions_cache_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_online_sessions_cache")),
    )
    op.create_index(op.f("ix_online_sessions_cache_user_id"), "online_sessions_cache", ["user_id"], unique=False)
    op.create_index(op.f("ix_online_sessions_cache_server_id"), "online_sessions_cache", ["server_id"], unique=False)
    op.create_index(op.f("ix_online_sessions_cache_remnawave_node_uuid"), "online_sessions_cache", ["remnawave_node_uuid"], unique=False)
    op.create_index(op.f("ix_online_sessions_cache_last_activity_at"), "online_sessions_cache", ["last_activity_at"], unique=False)
    op.create_index(op.f("ix_online_sessions_cache_observed_at"), "online_sessions_cache", ["observed_at"], unique=False)
    op.create_index(op.f("ix_online_sessions_cache_is_online"), "online_sessions_cache", ["is_online"], unique=False)

    op.create_table(
        "system_events",
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("level", sa.Enum("info", "warning", "error", name="eventlevel", native_enum=False, length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("server_id", sa.String(length=36), nullable=True),
        sa.Column("subscription_id", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], name=op.f("fk_system_events_server_id_servers"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], name=op.f("fk_system_events_subscription_id_subscriptions"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_system_events_user_id_users"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_system_events")),
    )
    op.create_index(op.f("ix_system_events_scope"), "system_events", ["scope"], unique=False)
    op.create_index(op.f("ix_system_events_level"), "system_events", ["level"], unique=False)
    op.create_index(op.f("ix_system_events_user_id"), "system_events", ["user_id"], unique=False)
    op.create_index(op.f("ix_system_events_server_id"), "system_events", ["server_id"], unique=False)
    op.create_index(op.f("ix_system_events_subscription_id"), "system_events", ["subscription_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_system_events_subscription_id"), table_name="system_events")
    op.drop_index(op.f("ix_system_events_server_id"), table_name="system_events")
    op.drop_index(op.f("ix_system_events_user_id"), table_name="system_events")
    op.drop_index(op.f("ix_system_events_level"), table_name="system_events")
    op.drop_index(op.f("ix_system_events_scope"), table_name="system_events")
    op.drop_table("system_events")

    op.drop_index(op.f("ix_online_sessions_cache_is_online"), table_name="online_sessions_cache")
    op.drop_index(op.f("ix_online_sessions_cache_observed_at"), table_name="online_sessions_cache")
    op.drop_index(op.f("ix_online_sessions_cache_last_activity_at"), table_name="online_sessions_cache")
    op.drop_index(op.f("ix_online_sessions_cache_remnawave_node_uuid"), table_name="online_sessions_cache")
    op.drop_index(op.f("ix_online_sessions_cache_server_id"), table_name="online_sessions_cache")
    op.drop_index(op.f("ix_online_sessions_cache_user_id"), table_name="online_sessions_cache")
    op.drop_table("online_sessions_cache")

    op.drop_index(op.f("ix_traffic_snapshots_snapshot_at"), table_name="traffic_snapshots")
    op.drop_index(op.f("ix_traffic_snapshots_server_id"), table_name="traffic_snapshots")
    op.drop_index(op.f("ix_traffic_snapshots_subscription_id"), table_name="traffic_snapshots")
    op.drop_index(op.f("ix_traffic_snapshots_user_id"), table_name="traffic_snapshots")
    op.drop_table("traffic_snapshots")

    op.drop_index(op.f("ix_trial_periods_ends_at"), table_name="trial_periods")
    op.drop_index(op.f("ix_trial_periods_status"), table_name="trial_periods")
    op.drop_index(op.f("ix_trial_periods_user_id"), table_name="trial_periods")
    op.drop_table("trial_periods")

    op.drop_index(op.f("ix_notifications_scheduled_for"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_status"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_notification_type"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_user_id"), table_name="notifications")
    op.drop_table("notifications")

    op.drop_index(op.f("ix_balance_transactions_admin_user_id"), table_name="balance_transactions")
    op.drop_index(op.f("ix_balance_transactions_topup_request_id"), table_name="balance_transactions")
    op.drop_index(op.f("ix_balance_transactions_subscription_id"), table_name="balance_transactions")
    op.drop_index(op.f("ix_balance_transactions_user_id"), table_name="balance_transactions")
    op.drop_table("balance_transactions")

    op.drop_index(op.f("ix_user_server_access_status"), table_name="user_server_access")
    op.drop_index(op.f("ix_user_server_access_server_id"), table_name="user_server_access")
    op.drop_index(op.f("ix_user_server_access_user_id"), table_name="user_server_access")
    op.drop_table("user_server_access")

    op.drop_index(op.f("ix_server_inbounds_remnawave_inbound_uuid"), table_name="server_inbounds")
    op.drop_index(op.f("ix_server_inbounds_server_id"), table_name="server_inbounds")
    op.drop_table("server_inbounds")

    op.drop_index(op.f("ix_subscriptions_grace_ends_at"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_next_billing_at"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_is_trial"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_is_current"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_status"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_plan_id"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_user_id"), table_name="subscriptions")
    op.drop_table("subscriptions")

    op.drop_index(op.f("ix_topup_requests_approved_by_admin_id"), table_name="topup_requests")
    op.drop_index(op.f("ix_topup_requests_status"), table_name="topup_requests")
    op.drop_index(op.f("ix_topup_requests_user_id"), table_name="topup_requests")
    op.drop_table("topup_requests")

    op.drop_index(op.f("ix_servers_name"), table_name="servers")
    op.drop_index(op.f("ix_servers_remnawave_node_uuid"), table_name="servers")
    op.drop_table("servers")

    op.drop_index(op.f("ix_users_status"), table_name="users")
    op.drop_index(op.f("ix_users_telegram_username"), table_name="users")
    op.drop_index(op.f("ix_users_telegram_id"), table_name="users")
    op.drop_table("users")

    op.drop_table("system_settings")

    op.drop_index(op.f("ix_plans_kind"), table_name="plans")
    op.drop_index(op.f("ix_plans_code"), table_name="plans")
    op.drop_table("plans")

    op.drop_index(op.f("ix_admin_users_username"), table_name="admin_users")
    op.drop_index(op.f("ix_admin_users_telegram_id"), table_name="admin_users")
    op.drop_table("admin_users")
