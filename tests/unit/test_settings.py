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
