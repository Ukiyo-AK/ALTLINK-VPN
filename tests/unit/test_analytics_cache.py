import asyncio
from unittest.mock import AsyncMock

import pytest

from altlink.presentation.web import analytics_cache
from altlink.presentation.web.analytics_cache import AnalyticsCache


@pytest.mark.asyncio
async def test_analytics_cache_coalesces_loads_and_returns_independent_data():
    cache = AnalyticsCache()
    loader = AsyncMock(return_value={"values": [1]})
    results = await asyncio.gather(*(cache.get("1d", loader) for _ in range(8)))
    loader.assert_awaited_once()
    results[0]["values"].append(99)
    assert await cache.get("1d", loader) == {"values": [1]}


@pytest.mark.asyncio
async def test_analytics_cache_expires_bounds_size_and_separates_filters(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(analytics_cache, "monotonic", lambda: clock[0])
    cache = AnalyticsCache(ttl_seconds=30, max_entries=2)
    loader = AsyncMock(return_value={"value": 1})
    await cache.get(("1d", "a"), loader)
    await cache.get(("1d", "b"), loader)
    await cache.get(("1w", "b"), loader)
    assert len(cache._entries) == 2
    assert ("1d", "a") not in cache._entries
    clock[0] = 131
    await cache.get(("1w", "b"), loader)
    assert loader.await_count == 4
    assert len(cache._entries) == 1


@pytest.mark.asyncio
async def test_analytics_failure_is_not_cached():
    cache = AnalyticsCache()
    loader = AsyncMock(side_effect=[RuntimeError("temporary failure"), {"ok": True}])
    with pytest.raises(RuntimeError):
        await cache.get("1d", loader)
    assert await cache.get("1d", loader) == {"ok": True}
    assert loader.await_count == 2


@pytest.mark.asyncio
async def test_analytics_snapshots_render_without_session(test_services):
    from types import SimpleNamespace
    from altlink.domain.enums import PlanCode
    from altlink.presentation.web.routes import templates
    from altlink.presentation.web.analytics_cache import business_snapshot, infrastructure_snapshot

    async with test_services.hub() as hub:
        user = await hub.accounts.get_or_create_user(telegram_id=11990, username="analytics_user", first_name="Test", last_name=None, language_code="ru")
        await hub.billing.activate_paid_plan(user.id, PlanCode.UNLIMITED, charge_user=False)
        await hub.dashboard.capture_server_metrics(force=True)
        overview = business_snapshot(await hub.dashboard.overview("1d"))
        servers = infrastructure_snapshot(await hub.dashboard.server_analytics("1d"))
    cache = AnalyticsCache()
    await cache.get("1d", AsyncMock(return_value=(overview, servers)))
    overview, servers = await cache.get("1d", AsyncMock(side_effect=AssertionError("Should be cached")))
    page = templates.env.get_template("analytics.html").render(
        admin=SimpleNamespace(full_name="Test", username="admin"), overview=overview,
        server_analytics=servers, charts={"business": overview["charts"], "servers": servers["charts"]},
        selected_period="1d", csrf_token="fresh-token",
    )
    assert "analytics_user" in page
    assert 'value="fresh-token"' in page
