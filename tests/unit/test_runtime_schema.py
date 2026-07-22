from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import BigInteger, inspect
from sqlalchemy.ext.asyncio import create_async_engine

from altlink.db import ensure_runtime_schema
from altlink.domain.enums import PlanCode
from altlink.infrastructure.db.models.base import enum_values
from altlink.infrastructure.db.models.ops import TrafficSnapshot


@pytest.mark.asyncio
async def test_runtime_schema_adds_missing_user_registration_columns(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    engine = create_async_engine(database_url, future=True)

    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                telegram_id BIGINT NOT NULL,
                username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                language_code VARCHAR(16),
                balance_rub NUMERIC(12, 2) NOT NULL DEFAULT 0,
                status VARCHAR(16) NOT NULL,
                remnawave_user_uuid VARCHAR(64),
                remnawave_username VARCHAR(64),
                remnawave_short_uuid VARCHAR(64),
                assigned_server_id TEXT,
                last_seen_at DATETIME
            )
            """
        )

    await ensure_runtime_schema(engine)

    async with engine.begin() as connection:
        column_names = await connection.run_sync(
            lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns("users")}
        )

    await engine.dispose()

    assert "registration_completed_at" in column_names
    assert "consent_accepted_at" in column_names
    assert "consent_version" in column_names
    assert "channel_verified_at" in column_names
    assert "promo_onboarding_completed_at" in column_names
    assert "vless_keys_downloaded_at" in column_names
    assert "hwid_device_count" in column_names
    assert "hwid_devices_checked_at" in column_names
    assert "traffic_limit_bytes_override" in column_names
    assert "traffic_limit_strategy_override" in column_names


@pytest.mark.asyncio
async def test_runtime_schema_backfills_promo_onboarding_for_existing_registered_users(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy_registered.db'}"
    engine = create_async_engine(database_url, future=True)

    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                telegram_id BIGINT NOT NULL,
                username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                language_code VARCHAR(16),
                balance_rub NUMERIC(12, 2) NOT NULL DEFAULT 0,
                status VARCHAR(16) NOT NULL,
                registration_completed_at DATETIME,
                consent_accepted_at DATETIME,
                consent_version VARCHAR(64),
                channel_verified_at DATETIME,
                remnawave_user_uuid VARCHAR(64),
                remnawave_username VARCHAR(64),
                remnawave_short_uuid VARCHAR(64),
                assigned_server_id TEXT,
                last_seen_at DATETIME
            )
            """
        )
        await connection.exec_driver_sql(
            """
            INSERT INTO users (
                id, created_at, updated_at, telegram_id, username, balance_rub, status,
                registration_completed_at, consent_accepted_at, consent_version
            )
            VALUES (
                'u-1', '2026-01-01 00:00:00', '2026-01-01 00:00:00', 1001, 'legacy_user', 0, 'new',
                '2026-01-02 00:00:00', '2026-01-02 00:00:00', 'placeholder-v1'
            )
            """
        )

    await ensure_runtime_schema(engine)

    async with engine.begin() as connection:
        promo_completed_at = await connection.exec_driver_sql(
            "SELECT promo_onboarding_completed_at FROM users WHERE id = 'u-1'"
        )
        value = promo_completed_at.scalar_one()

    await engine.dispose()

    assert value is not None


def test_enum_values_tracks_longest_enum_member_length():
    enum_type = enum_values(PlanCode)

    assert enum_type.length == max(len(member.value) for member in PlanCode)


def test_traffic_snapshot_uses_bigint_for_large_usage_values():
    assert isinstance(TrafficSnapshot.__table__.c.used_bytes.type, BigInteger)
    assert isinstance(TrafficSnapshot.__table__.c.lifetime_used_bytes.type, BigInteger)


@pytest.mark.asyncio
async def test_runtime_schema_adds_missing_topup_provider_columns(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy_topups.db'}"
    engine = create_async_engine(database_url, future=True)

    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                telegram_id BIGINT NOT NULL,
                username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                language_code VARCHAR(16),
                balance_rub NUMERIC(12, 2) NOT NULL DEFAULT 0,
                status VARCHAR(16) NOT NULL
            )
            """
        )
        await connection.exec_driver_sql(
            """
            CREATE TABLE topup_requests (
                id TEXT PRIMARY KEY,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                user_id TEXT NOT NULL,
                amount_rub NUMERIC(12, 2) NOT NULL,
                status VARCHAR(16) NOT NULL,
                user_comment TEXT,
                admin_comment TEXT,
                approved_by_admin_id TEXT,
                approved_at DATETIME,
                rejected_at DATETIME,
                canceled_at DATETIME
            )
            """
        )

    await ensure_runtime_schema(engine)

    async with engine.begin() as connection:
        column_names = await connection.run_sync(
            lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns("topup_requests")}
        )

    await engine.dispose()

    assert "provider_code" in column_names
    assert "external_payment_id" in column_names
    assert "external_payment_url" in column_names


@pytest.mark.asyncio
async def test_runtime_schema_adds_missing_server_inbound_access_type(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy_inbounds.db'}"
    engine = create_async_engine(database_url, future=True)

    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                telegram_id BIGINT NOT NULL,
                username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                language_code VARCHAR(16),
                balance_rub NUMERIC(12, 2) NOT NULL DEFAULT 0,
                status VARCHAR(16) NOT NULL
            )
            """
        )
        await connection.exec_driver_sql(
            """
            CREATE TABLE server_inbounds (
                id TEXT PRIMARY KEY,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                server_id TEXT NOT NULL,
                remnawave_inbound_uuid VARCHAR(64),
                tag VARCHAR(255) NOT NULL,
                type VARCHAR(64) NOT NULL,
                network VARCHAR(64),
                security VARCHAR(64),
                port INTEGER,
                client_count INTEGER NOT NULL DEFAULT 0,
                max_clients INTEGER NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                raw_payload JSON
            )
            """
        )

    await ensure_runtime_schema(engine)

    async with engine.begin() as connection:
        column_names = await connection.run_sync(
            lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns("server_inbounds")}
        )

    await engine.dispose()

    assert "access_type" in column_names


@pytest.mark.asyncio
async def test_runtime_schema_adds_personal_promo_owner_column(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy_promos.db'}"
    engine = create_async_engine(database_url, future=True)

    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                telegram_id BIGINT NOT NULL,
                balance_rub NUMERIC(12, 2) NOT NULL DEFAULT 0,
                status VARCHAR(16) NOT NULL
            )
            """
        )
        await connection.exec_driver_sql(
            """
            CREATE TABLE promo_codes (
                id TEXT PRIMARY KEY,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                code VARCHAR(64) NOT NULL,
                name VARCHAR(255) NOT NULL,
                reward_kind VARCHAR(32) NOT NULL,
                reward_value NUMERIC(12, 2) NOT NULL,
                usage_limit INTEGER,
                used_count INTEGER NOT NULL DEFAULT 0,
                expires_at DATETIME,
                new_users_only BOOLEAN NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_by_admin_id TEXT
            )
            """
        )

    await ensure_runtime_schema(engine)

    async with engine.begin() as connection:
        column_names = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns("promo_codes")
            }
        )

    await engine.dispose()

    assert "assigned_user_id" in column_names
