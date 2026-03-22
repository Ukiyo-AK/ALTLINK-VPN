from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = Field(default="development", alias="APP_ENV")
    app_name: str = Field(default="ALTLINK VPN", alias="APP_NAME")
    debug: bool = Field(default=False, alias="DEBUG")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    json_logs: bool = Field(default=True, alias="JSON_LOGS")
    secret_key: str = Field(alias="SECRET_KEY")
    session_cookie_name: str = Field(default="altlink_admin_session", alias="SESSION_COOKIE_NAME")
    session_max_age_seconds: int = Field(default=43200, alias="SESSION_MAX_AGE_SECONDS")
    public_base_url: str = Field(alias="PUBLIC_BASE_URL")
    admin_panel_base_url: str = Field(alias="ADMIN_PANEL_BASE_URL")
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    timezone: str = Field(default="UTC", alias="TIMEZONE")

    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(alias="REDIS_URL")

    remwave_base_url: str = Field(alias="REMWAVE_BASE_URL")
    remwave_api_token: str = Field(alias="REMWAVE_API_TOKEN")
    remwave_timeout_seconds: int = Field(default=20, alias="REMWAVE_TIMEOUT_SECONDS")
    remwave_max_retries: int = Field(default=3, alias="REMWAVE_MAX_RETRIES")
    remwave_verify_tls: bool = Field(default=True, alias="REMWAVE_VERIFY_TLS")

    client_bot_token: str = Field(alias="CLIENT_BOT_TOKEN")
    admin_bot_token: str = Field(alias="ADMIN_BOT_TOKEN")
    admin_telegram_ids_raw: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")
    bot_heartbeat_interval_seconds: int = Field(default=30, alias="BOT_HEARTBEAT_INTERVAL_SECONDS")

    trial_duration_days: int = Field(default=2, alias="TRIAL_DURATION_DAYS")
    grace_period_days: int = Field(default=14, alias="GRACE_PERIOD_DAYS")
    low_balance_threshold_rub: int = Field(default=50, alias="LOW_BALANCE_THRESHOLD_RUB")
    low_balance_notify_days: int = Field(default=5, alias="LOW_BALANCE_NOTIFY_DAYS")
    traffic_notify_thresholds_raw: str = Field(default="70,90,100", alias="TRAFFIC_NOTIFY_THRESHOLDS")

    rate_limit_login: str = Field(default="10/minute", alias="RATE_LIMIT_LOGIN")
    rate_limit_api: str = Field(default="60/minute", alias="RATE_LIMIT_API")

    @field_validator("admin_telegram_ids_raw", "traffic_notify_thresholds_raw", mode="before")
    @classmethod
    def _strip_csv(cls, value: str | None) -> str:
        return (value or "").strip()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def admin_telegram_ids(self) -> list[int]:
        return [int(item.strip()) for item in self.admin_telegram_ids_raw.split(",") if item.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def traffic_notify_thresholds(self) -> list[int]:
        thresholds = [int(item.strip()) for item in self.traffic_notify_thresholds_raw.split(",") if item.strip()]
        return sorted(set(thresholds))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

