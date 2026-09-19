from unittest.mock import AsyncMock
from html import unescape

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from altlink.presentation.web import routes


def subscription_app(services):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="subscription-test-secret")
    app.state.container = services
    app.state.settings = services.settings
    app.include_router(routes.router)
    return app


async def create_trial(services):
    async with services.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=551100, username="private_account_name", first_name="Private",
            last_name="User", language_code="ru",
        )
        await hub.billing.activate_trial(user.id)
        return user.id, user.remnawave_short_uuid


@pytest.mark.asyncio
async def test_connect_http_keeps_original_link_and_revokes_old_local_urls(test_services, monkeypatch):
    test_services.settings.backend_public_url = "https://altlink.online"
    user_id, token = await create_trial(test_services)
    upstream = AsyncMock(return_value=httpx.Response(200, content=b"vless://server-key", headers={"content-type": "text/plain"}))
    monkeypatch.setattr(routes, "fetch_upstream_subscription", upstream)
    app = subscription_app(test_services)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://altlink.online") as client:
        async with test_services.hub() as hub:
            bundle = await hub.accounts.get_subscription_bundle(user_id)
        assert bundle["subscription_url"] == f"https://sub.example/{token}"
        assert bundle["subscription_connect_url"] == f"https://altlink.online/connect/{token}"

        page = await client.get(f"/connect/{token}")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "no-store" in page.headers["cache-control"]
        assert f'href="happ://add/https://altlink.online/sub/{token}"' in page.text
        assert f'href="incy://import/https://altlink.online/sub/{token}"' in page.text
        assert f'value="https://altlink.online/sub/{token}"' in page.text
        assert 'name="referrer" content="no-referrer"' in page.text
        assert "private_account_name" not in page.text
        upstream.assert_not_awaited()

        mirrored = await client.get(f"/sub/{token}")
        assert mirrored.content == b"vless://server-key"
        assert mirrored.headers["content-type"] == "text/plain"
        assert "sandbox" in mirrored.headers["content-security-policy"]

        async with test_services.hub() as hub:
            await hub.accounts.revoke_user_subscription_link(user_id)
            new_bundle = await hub.accounts.get_subscription_bundle(user_id)
            new_token = new_bundle["user"].remnawave_short_uuid
        assert new_token != token
        assert new_bundle["subscription_url"] == f"https://sub.example/{new_token}"
        upstream.reset_mock()
        for path in (f"/sub/{token}", f"/connect/{token}"):
            assert (await client.get(path)).status_code == 404
        upstream.assert_not_awaited()
        fresh_page = await client.get(f"/connect/{new_token}")
        assert fresh_page.status_code == 200
        assert f"https://altlink.online/sub/{new_token}" in fresh_page.text
        assert token not in fresh_page.text
        assert (await client.get(f"/sub/{new_token}")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("agent,choice,expected", [
    ("iPhone OS 18 like Mac OS X", "", "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"),
    ("Linux; Android 15", "", "https://play.google.com/store/apps/details?id=com.happproxy&hl=ru"),
    ("Windows NT 10.0", "?platform=ios", "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"),
    ("iPhone OS 18", "?platform=windows", "https://www.happ.su/main/ru"),
    ("Android 15", "?platform=invalid", "https://play.google.com/store/apps/details?id=com.happproxy&hl=ru"),
])
async def test_connect_renders_platform_links_even_without_javascript(test_services, agent, choice, expected):
    _, token = await create_trial(test_services)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(subscription_app(test_services)), base_url="https://altlink.online") as client:
        response = await client.get(f"/connect/{token}{choice}", headers={"user-agent": agent})
    assert response.status_code == 200
    assert f'href="{expected}" data-app-download' in unescape(response.text)
    assert 'name="platform"' in response.text
    assert all(f'value="{platform}"' in response.text for platform in ("auto", "ios", "android", "windows", "macos", "linux", "other"))
    assert '/static/subscription_connect.js?v=' in response.text
    assert 'rel="noopener noreferrer"' in response.text
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,upstream_status", [("GET", 200), ("HEAD", 200), ("GET", 304), ("GET", 403), ("GET", 404), ("GET", 503)])
async def test_mirror_http_preserves_status_payload_and_metadata(test_services, monkeypatch, method, upstream_status):
    _, token = await create_trial(test_services)
    body = b"" if upstream_status == 304 else b"opaque-subscription-payload"
    upstream = AsyncMock(return_value=httpx.Response(upstream_status, content=body, headers={
        "subscription-userinfo": "upload=10; download=20; total=100; expire=1900000000",
        "etag": '"revision-1"', "profile-update-interval": "1", "x-hwid-limit": "true",
        "set-cookie": "upstream_secret=hidden", "content-type": "text/plain",
    }))
    monkeypatch.setattr(routes, "fetch_upstream_subscription", upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(subscription_app(test_services)), base_url="https://altlink.online") as client:
        response = await client.request(method, f"/sub/{token}/happ?tag=one&tag=two", headers={
            "x-hwid": "device-test-1234", "x-device-os": "iOS", "x-ver-os": "18.3",
            "if-none-match": '"revision-1"', "cookie": "session=private", "authorization": "Bearer private",
        })
    assert response.status_code == upstream_status
    assert response.content == (b"" if method == "HEAD" else body)
    if method == "HEAD":
        assert response.headers["content-length"] == str(len(body))
    assert response.headers["etag"] == '"revision-1"'
    assert response.headers["x-hwid-limit"] == "true"
    assert response.headers["profile-update-interval"] == "1"
    assert response.headers["subscription-userinfo"].endswith("expire=1900000000")
    assert "set-cookie" not in response.headers
    assert "no-store" in response.headers["cache-control"]
    args = upstream.await_args
    assert args.args[1].endswith(f"/api/sub/{token}/happ")
    assert args.kwargs["headers"]["x-hwid"] == "device-test-1234"
    assert args.kwargs["headers"]["x-ver-os"] == "18.3"
    assert args.kwargs["headers"]["if-none-match"] == '"revision-1"'
    assert "cookie" not in args.kwargs["headers"]
    assert "authorization" not in args.kwargs["headers"]
    assert args.kwargs["query"] == [("tag", "one"), ("tag", "two")]


@pytest.mark.asyncio
async def test_upstream_timeout_does_not_break_connection_page_or_leak_token(test_services, monkeypatch, caplog):
    _, token = await create_trial(test_services)
    upstream = AsyncMock(side_effect=httpx.ReadTimeout(f"secret upstream URL /sub/{token}"))
    monkeypatch.setattr(routes, "fetch_upstream_subscription", upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(subscription_app(test_services)), base_url="https://altlink.online") as client:
        response = await client.get(f"/sub/{token}")
        page = await client.get(f"/connect/{token}")
    assert response.status_code == 502
    assert token not in response.text
    assert token not in caplog.text
    assert page.status_code == 200


@pytest.mark.asyncio
async def test_mirror_self_reference_is_rejected_without_remote_request(test_services, monkeypatch):
    test_services.settings.remnawave_subscription_base_url = test_services.settings.backend_public_url + "/sub"
    _, token = await create_trial(test_services)
    upstream = AsyncMock()
    monkeypatch.setattr(routes, "fetch_upstream_subscription", upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(subscription_app(test_services)), base_url="https://altlink.online") as client:
        response = await client.get(f"/sub/{token}")
    assert response.status_code == 503
    upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/sub/unknown-token", "/connect/unknown-token", "/sub/invalid%20token", "/connect/abc", "/sub/valid-token/bad%20format"])
async def test_unknown_or_invalid_tokens_do_not_trigger_remote_calls(test_services, monkeypatch, path):
    upstream = AsyncMock()
    monkeypatch.setattr(routes, "fetch_upstream_subscription", upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(subscription_app(test_services)), base_url="https://altlink.online") as client:
        response = await client.get(path)
    assert response.status_code == 404
    upstream.assert_not_awaited()


@pytest.mark.parametrize("client_type", [".", "..", "../info", "%2e%2e", "happ?url=another"])
def test_client_format_cannot_escape_subscription_path(client_type):
    with pytest.raises(routes.HTTPException) as error:
        routes.validate_subscription_path("valid-token", client_type)
    assert error.value.status_code == 404
