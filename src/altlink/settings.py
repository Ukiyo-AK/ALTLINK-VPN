from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url


DEFAULT_DATABASE_URL = "postgresql+asyncpg://altlink:altlink@postgres:5432/altlink"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "ALTLINK VPN"
    environment: str = "development"
    debug: bool = False
    timezone: str = "UTC"
    log_to_file: bool = True
    log_dir: str = "logs"
    log_file_max_bytes: int = 5 * 1024 * 1024
    log_file_backup_count: int = 5

    secret_key: str = "change-me"
    session_secret_key: str = "change-me-session"
    admin_api_key: str = "change-me-admin-api-key"

    database_url: str = DEFAULT_DATABASE_URL
    sql_echo: bool = False

    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    backend_public_url: str = "http://localhost:8000"

    client_bot_token: str = ""
    admin_bot_token: str = ""
    admin_allowed_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    client_bot_name: str = "altlink"
    admin_bot_name: str = "admin altlink bot"
    required_subscription_channel: str = "@altlink_channel"
    required_subscription_channel_url: str = "https://t.me/altlink_channel"
    support_username: str = "@altlink_support"
    user_agreement_telegraph_url: str = "https://telegra.ph/ALTLINK-VPN--Polzovatelskoe-soglashenie-04-22"
    privacy_policy_telegraph_url: str = "https://telegra.ph/ALTLINK-VPN--Politika-konfidencialnosti-04-22"
    telegram_auth_max_age_seconds: int = 300

    remnawave_base_url: str = ""
    remnawave_api_token: str = ""
    remnawave_timeout_seconds: int = 20
    remnawave_retry_attempts: int = 3
    remnawave_subscription_base_url: str = ""

    default_currency: str = "RUB"
    trial_duration_days: int = 2
    billing_period_days: int = 30
    grace_period_days: int = 14
    low_balance_threshold_rub: int = 50
    unlimited_plan_price_rub: int = 199
    single_server_plan_price_rub: int = 69
    whitelist_price_per_gb_rub: int = 2
    payment_provider: str = "manual"
    yookassa_api_base_url: str = "https://api.yookassa.ru/v3"
    yookassa_shop_id: str = Field(
        default="",
        validation_alias=AliasChoices("YOOKASSA_SHOP_ID", "YOOKASSA_SHOPID"),
    )
    yookassa_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("YOOKASSA_SECRET_KEY", "YOOKASSA_API_KEY"),
    )
    yookassa_return_url: str = ""
    yookassa_timeout_seconds: int = 20
    traffic_notification_thresholds: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [70, 90, 100]
    )

    sync_servers_interval_minutes: int = 30
    billing_interval_minutes: int = 5
    traffic_snapshot_interval_minutes: int = 10
    notification_dispatch_interval_minutes: int = 2
    online_refresh_interval_minutes: int = 2
    remnawave_healthcheck_interval_minutes: int = 5
    server_latency_monitor_interval_minutes: int = 360
    user_abuse_monitor_interval_minutes: int = 5
    user_abuse_unique_ip_threshold: int = 5
    user_abuse_ip_fetch_poll_attempts: int = 5
    user_abuse_ip_fetch_poll_delay_seconds: float = 1.0
    user_abuse_hwid_fetch_concurrency: int = 8
    vless_keys_download_cooldown_seconds: int = 300
    latency_probe_scheme: str = "https"
    latency_probe_port: int = 44443
    latency_probe_path: str = "/ping"
    browser_latency_timeout_ms: int = 4000
    heartbeat_max_age_seconds: int = 180

    @field_validator("required_subscription_channel", mode="before")
    @classmethod
    def _normalize_required_channel(cls, value: object) -> str:
        if value is None:
            return "@altlink_channel"
        if isinstance(value, str) and not value.strip():
            return "@altlink_channel"
        raw = str(value).strip()
        if raw.startswith("https://t.me/") or raw.startswith("http://t.me/"):
            raw = raw.split("://", 1)[-1].split("/", 1)[-1].strip("/")
        if raw.startswith("@") or raw.startswith("-100") or raw.lstrip("-").isdigit():
            return raw
        return f"@{raw.lstrip('@')}"

    @field_validator("required_subscription_channel_url", mode="before")
    @classmethod
    def _normalize_required_channel_url(cls, value: object) -> str:
        if value is None:
            return "https://t.me/altlink_channel"
        if isinstance(value, str) and not value.strip():
            return "https://t.me/altlink_channel"
        raw = str(value).strip()
        if raw.startswith("https://") or raw.startswith("http://"):
            return raw
        return f"https://t.me/{raw.lstrip('@')}"

    @field_validator("admin_allowed_telegram_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> list[int]:
        if value in (None, "", []):
            return []
        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [int(item) for item in value]
        raise ValueError("ADMIN_ALLOWED_TELEGRAM_IDS must be a comma-separated list")

    @field_validator("debug", mode="before")
    @classmethod
    def _parse_debug(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on", "debug", "dev", "development"}:
                return True
            if normalized in {"0", "false", "no", "off", "release", "prod", "production"}:
                return False
        return bool(value)

    @field_validator("traffic_notification_thresholds", mode="before")
    @classmethod
    def _parse_thresholds(cls, value: object) -> list[int]:
        if value in (None, "", []):
            return [70, 90, 100]
        if isinstance(value, str):
            items = [int(item.strip()) for item in value.split(",") if item.strip()]
            return sorted(set(items))
        if isinstance(value, list):
            return sorted({int(item) for item in value})
        raise ValueError("TRAFFIC_NOTIFICATION_THRESHOLDS must be a comma-separated list")

    @field_validator("payment_provider", mode="before")
    @classmethod
    def _normalize_payment_provider(cls, value: object) -> str:
        if value is None:
            return "manual"
        normalized = str(value).strip().lower()
        if normalized == "wata":
            return "yookassa"
        if normalized in {"", "stub", "manual", "yookassa"}:
            return normalized or "manual"
        raise ValueError("PAYMENT_PROVIDER must be one of: stub, manual, yookassa")

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_database_url(cls, value: object) -> str:
        if value is None:
            return DEFAULT_DATABASE_URL
        raw = str(value).strip()
        if not raw:
            return DEFAULT_DATABASE_URL
        if raw.startswith("postgres://"):
            return f"postgresql+asyncpg://{raw[len('postgres://'):]}"
        if raw.startswith("postgresql://"):
            return f"postgresql+asyncpg://{raw[len('postgresql://'):]}"
        return raw

    @field_validator("latency_probe_scheme", mode="before")
    @classmethod
    def _normalize_latency_probe_scheme(cls, value: object) -> str:
        raw = str(value or "https").strip().lower()
        return raw or "https"

    @field_validator("latency_probe_path", mode="before")
    @classmethod
    def _normalize_latency_probe_path(cls, value: object) -> str:
        raw = str(value or "/ping").strip()
        if not raw:
            return "/ping"
        return raw if raw.startswith("/") else f"/{raw}"

    @model_validator(mode="after")
    def _require_postgresql_in_production(self) -> Settings:
        drivername = make_url(self.database_url).drivername
        if self.environment == "production" and drivername.startswith("sqlite"):
            raise ValueError("Production deployment must use PostgreSQL instead of SQLite.")
        return self

    @property
    def remnawave_subscription_public_base(self) -> str:
        return (self.remnawave_subscription_base_url or self.remnawave_base_url).rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
