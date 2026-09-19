from types import SimpleNamespace

from altlink.utils.subscriptions import (
    local_subscription_connect_url,
    local_subscription_proxy_url,
    remnawave_public_subscription_url,
)


def test_subscription_urls_use_local_domain_and_keep_remnawave_as_upstream():
    settings = SimpleNamespace(
        backend_public_url="https://altlink.online/",
        remnawave_subscription_base_url="https://sub-manager.altlink.online/api/sub",
        remnawave_base_url="https://panel.altlink.online",
    )

    assert local_subscription_proxy_url(settings, "abc_DEF-123") == "https://altlink.online/sub/abc_DEF-123"
    assert local_subscription_connect_url(settings, "abc_DEF-123") == "https://altlink.online/connect/abc_DEF-123"
    assert remnawave_public_subscription_url(settings, "abc_DEF-123") == (
        "https://sub-manager.altlink.online/api/sub/abc_DEF-123"
    )


def test_subscription_urls_reject_invalid_public_base():
    settings = SimpleNamespace(
        backend_public_url="altlink.online",
        remnawave_subscription_base_url="",
        remnawave_base_url="",
    )

    assert local_subscription_proxy_url(settings, "abc123") is None
    assert local_subscription_connect_url(settings, "abc123") is None
    assert remnawave_public_subscription_url(settings, "abc123") is None
