from __future__ import annotations

import pytest

from altlink.settings import Settings


def test_settings_parse_comma_separated_list_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ADMIN_ALLOWED_TELEGRAM_IDS=1698986089,123456789",
                "TRAFFIC_NOTIFICATION_THRESHOLDS=70,90,100",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.admin_allowed_telegram_ids == [1698986089, 123456789]
    assert settings.traffic_notification_thresholds == [70, 90, 100]


def test_settings_restore_default_required_channel_when_env_value_is_blank(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "REQUIRED_SUBSCRIPTION_CHANNEL=",
                "REQUIRED_SUBSCRIPTION_CHANNEL_URL=",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.required_subscription_channel == "@altlink_channel"
    assert settings.required_subscription_channel_url == "https://t.me/altlink_channel"


def test_settings_normalize_channel_without_at_prefix(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "REQUIRED_SUBSCRIPTION_CHANNEL=altlink_channel",
                "REQUIRED_SUBSCRIPTION_CHANNEL_URL=altlink_channel",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.required_subscription_channel == "@altlink_channel"
    assert settings.required_subscription_channel_url == "https://t.me/altlink_channel"


def test_settings_use_postgresql_asyncpg_by_default(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+asyncpg://altlink:altlink@postgres:5432/altlink"


def test_settings_normalize_plain_postgres_database_url(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://demo:secret@db.internal:5432/altlink", encoding="utf-8")

    settings = Settings(_env_file=env_file)

    assert settings.database_url == "postgresql+asyncpg://demo:secret@db.internal:5432/altlink"


def test_settings_reject_sqlite_in_production(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ENVIRONMENT=production",
                "DATABASE_URL=sqlite+aiosqlite:///./data/altlink.db",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Production deployment must use PostgreSQL"):
        Settings(_env_file=env_file)


def test_settings_normalize_browser_latency_probe_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "LATENCY_PROBE_SCHEME=HTTPS",
                "LATENCY_PROBE_PATH=ping",
                "LATENCY_PROBE_PORT=44443",
                "BROWSER_LATENCY_TIMEOUT_MS=3500",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.latency_probe_scheme == "https"
    assert settings.latency_probe_path == "/ping"
    assert settings.latency_probe_port == 44443
    assert settings.browser_latency_timeout_ms == 3500


def test_settings_normalize_legacy_wata_provider_to_yookassa(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PAYMENT_PROVIDER=wata", encoding="utf-8")

    settings = Settings(_env_file=env_file)

    assert settings.payment_provider == "yookassa"
