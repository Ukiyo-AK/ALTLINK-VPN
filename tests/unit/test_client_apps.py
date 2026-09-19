from urllib.parse import parse_qs, urlparse

import pytest

from altlink.utils.client_apps import CLIENT_PLATFORMS, client_downloads_for_platform, detect_client_platform


@pytest.mark.parametrize("agent,expected", [
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)", "ios"),
    ("Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)", "ios"),
    ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit Telegram-Android", "android"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "windows"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)", "macos"),
    ("Mozilla/5.0 (X11; Linux x86_64)", "linux"),
    ("Mozilla/5.0 (X11; CrOS x86_64 15633)", "other"),
    ("", "other"),
])
def test_platform_detection(agent, expected):
    assert detect_client_platform(agent) == expected


@pytest.mark.parametrize("platform", CLIENT_PLATFORMS)
def test_downloads_are_official_https_urls(platform):
    downloads = client_downloads_for_platform(platform)
    assert set(downloads) == {"happ", "incy"}
    for link in downloads.values():
        parsed = urlparse(link["url"])
        assert parsed.scheme == "https"
        assert parsed.hostname in {"www.happ.su", "github.com", "play.google.com", "apps.apple.com"}


def test_mobile_stores_are_localized_without_changing_desktop_links():
    ios = client_downloads_for_platform("ios")
    assert "/ru/app/incy/" in ios["incy"]["url"]
    assert "российском магазине" in ios["happ"]["note"]
    for link in client_downloads_for_platform("android").values():
        assert parse_qs(urlparse(link["url"]).query)["hl"] == ["ru"]
    for platform in ("windows", "macos", "linux", "unknown"):
        assert client_downloads_for_platform(platform) == client_downloads_for_platform("other")
