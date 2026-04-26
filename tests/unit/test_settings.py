from __future__ import annotations

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
