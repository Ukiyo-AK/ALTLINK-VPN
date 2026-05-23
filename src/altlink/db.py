from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.engine import make_url

from altlink.infrastructure.db.models.base import Base
from altlink.settings import Settings
from altlink.domain.enums import PlanCode

logger = logging.getLogger(__name__)


def ensure_sqlite_directory(database_url: str) -> None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return
    if not url.database or url.database == ":memory:":
        return

    database_path = Path(url.database)
    database_path.parent.mkdir(parents=True, exist_ok=True)


def create_engine(settings: Settings) -> AsyncEngine:
    ensure_sqlite_directory(settings.database_url)
    url = make_url(settings.database_url)
    engine_kwargs = {
        "echo": settings.sql_echo,
        "future": True,
        "pool_pre_ping": True,
    }
    if not url.drivername.startswith("sqlite"):
        engine_kwargs["pool_recycle"] = 1800
    return create_async_engine(settings.database_url, **engine_kwargs)


def create_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(create_engine(settings), expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


def _ensure_runtime_schema_sync(connection) -> None:
    Base.metadata.create_all(bind=connection)

    inspector = inspect(connection)
    table_names = inspector.get_table_names()
    if "users" not in table_names:
        return

    existing_columns = {column["name"] for column in inspector.get_columns("users")}
    expected_columns = {
        "registration_completed_at": DateTime(timezone=True),
        "consent_accepted_at": DateTime(timezone=True),
        "consent_version": String(64),
        "channel_verified_at": DateTime(timezone=True),
        "promo_onboarding_completed_at": DateTime(timezone=True),
        "referral_code": String(32),
        "referred_by_user_id": String(36),
        "referral_reward_granted_at": DateTime(timezone=True),
    }

    for column_name, column_type in expected_columns.items():
        if column_name in existing_columns:
            continue
        compiled_type = column_type.compile(dialect=connection.dialect)
        connection.execute(text(f"ALTER TABLE users ADD COLUMN {column_name} {compiled_type}"))
        logger.warning("Added missing column %s to users table during runtime schema preparation.", column_name)
        if column_name == "promo_onboarding_completed_at":
            connection.execute(
                text(
                    """
                    UPDATE users
                    SET promo_onboarding_completed_at = COALESCE(registration_completed_at, consent_accepted_at)
                    WHERE promo_onboarding_completed_at IS NULL
                      AND (registration_completed_at IS NOT NULL OR consent_accepted_at IS NOT NULL)
                    """
                )
            )
            logger.warning(
                "Backfilled promo_onboarding_completed_at for already registered users during runtime schema preparation."
            )

    if "plans" in table_names:
        plan_columns = {column["name"] for column in inspector.get_columns("plans")}
        expected_plan_columns = {"device_limit": Integer()}
        for column_name, column_type in expected_plan_columns.items():
            if column_name in plan_columns:
                continue
            compiled_type = column_type.compile(dialect=connection.dialect)
            connection.execute(text(f"ALTER TABLE plans ADD COLUMN {column_name} {compiled_type}"))
            logger.warning("Added missing column %s to plans table during runtime schema preparation.", column_name)

        plan_code_column = next((column for column in inspector.get_columns("plans") if column["name"] == "code"), None)
        required_plan_code_length = max(len(member.value) for member in PlanCode)
        current_plan_code_length = getattr(getattr(plan_code_column, "get", lambda *_: None)("type"), "length", None)
        if (
            connection.dialect.name != "sqlite"
            and current_plan_code_length is not None
            and current_plan_code_length < required_plan_code_length
        ):
            connection.execute(text(f"ALTER TABLE plans ALTER COLUMN code TYPE VARCHAR({required_plan_code_length})"))
            logger.warning(
                "Expanded plans.code length from %s to %s during runtime schema preparation.",
                current_plan_code_length,
                required_plan_code_length,
            )

    if "topup_requests" in table_names:
        topup_columns = {column["name"] for column in inspector.get_columns("topup_requests")}
        expected_topup_columns = {
            "provider_code": String(32),
            "external_payment_id": String(128),
            "external_payment_url": Text(),
        }
        for column_name, column_type in expected_topup_columns.items():
            if column_name in topup_columns:
                continue
            compiled_type = column_type.compile(dialect=connection.dialect)
            connection.execute(text(f"ALTER TABLE topup_requests ADD COLUMN {column_name} {compiled_type}"))
            logger.warning(
                "Added missing column %s to topup_requests table during runtime schema preparation.",
                column_name,
            )

    if "traffic_snapshots" in table_names and connection.dialect.name != "sqlite":
        traffic_columns = {column["name"]: column for column in inspector.get_columns("traffic_snapshots")}
        for column_name in ("used_bytes", "lifetime_used_bytes"):
            column = traffic_columns.get(column_name)
            if column is None:
                continue
            compiled_type = column["type"].compile(dialect=connection.dialect).upper()
            if compiled_type == BigInteger().compile(dialect=connection.dialect).upper():
                continue
            connection.execute(text(f"ALTER TABLE traffic_snapshots ALTER COLUMN {column_name} TYPE BIGINT"))
            logger.warning(
                "Expanded traffic_snapshots.%s from %s to BIGINT during runtime schema preparation.",
                column_name,
                compiled_type,
            )


async def ensure_runtime_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_ensure_runtime_schema_sync)
