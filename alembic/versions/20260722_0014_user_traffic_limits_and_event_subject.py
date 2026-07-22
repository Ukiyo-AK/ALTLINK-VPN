"""add per-user traffic limits and event subjects

Revision ID: 20260722_0014
Revises: 20260716_0013
Create Date: 2026-07-22 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260722_0014"
down_revision = "20260716_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "users" in tables:
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        if "traffic_limit_bytes_override" not in user_columns:
            op.add_column("users", sa.Column("traffic_limit_bytes_override", sa.BigInteger(), nullable=True))
        if "traffic_limit_strategy_override" not in user_columns:
            op.add_column("users", sa.Column("traffic_limit_strategy_override", sa.String(16), nullable=True))

    if "system_events" in tables:
        event_columns = {column["name"] for column in inspector.get_columns("system_events")}
        if "subject_user_id" not in event_columns:
            op.add_column("system_events", sa.Column("subject_user_id", sa.String(36), nullable=True))
        event_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("system_events")}
        if "ix_system_events_subject_user_id" not in event_indexes:
            op.create_index(
                "ix_system_events_subject_user_id",
                "system_events",
                ["subject_user_id"],
                unique=False,
            )
        if bind.dialect.name == "postgresql":
            bind.execute(
                sa.text(
                    """
                    UPDATE system_events
                    SET subject_user_id = payload ->> 'user_id'
                    WHERE subject_user_id IS NULL
                      AND payload IS NOT NULL
                      AND payload ->> 'user_id' IS NOT NULL
                    """
                )
            )
        elif bind.dialect.name == "sqlite":
            bind.execute(
                sa.text(
                    """
                    UPDATE system_events
                    SET subject_user_id = json_extract(payload, '$.user_id')
                    WHERE subject_user_id IS NULL
                      AND payload IS NOT NULL
                      AND json_extract(payload, '$.user_id') IS NOT NULL
                    """
                )
            )

    if "traffic_snapshots" in tables:
        traffic_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("traffic_snapshots")}
        if "ix_traffic_snapshots_user_server_created" not in traffic_indexes:
            op.create_index(
                "ix_traffic_snapshots_user_server_created",
                "traffic_snapshots",
                ["user_id", "server_id", "created_at"],
                unique=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "system_events" in tables:
        event_indexes = {index["name"] for index in inspector.get_indexes("system_events")}
        if "ix_system_events_subject_user_id" in event_indexes:
            op.drop_index("ix_system_events_subject_user_id", table_name="system_events")
        event_columns = {column["name"] for column in inspector.get_columns("system_events")}
        if "subject_user_id" in event_columns:
            op.drop_column("system_events", "subject_user_id")

    if "traffic_snapshots" in tables:
        traffic_indexes = {index["name"] for index in inspector.get_indexes("traffic_snapshots")}
        if "ix_traffic_snapshots_user_server_created" in traffic_indexes:
            op.drop_index("ix_traffic_snapshots_user_server_created", table_name="traffic_snapshots")

    if "users" in tables:
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        if "traffic_limit_strategy_override" in user_columns:
            op.drop_column("users", "traffic_limit_strategy_override")
        if "traffic_limit_bytes_override" in user_columns:
            op.drop_column("users", "traffic_limit_bytes_override")
