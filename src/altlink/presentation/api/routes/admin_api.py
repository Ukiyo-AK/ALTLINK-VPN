from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from altlink.application.services.registry import ServiceHub
from altlink.presentation.api.dependencies import get_hub, require_admin_api_key

router = APIRouter(prefix="/api/v1", tags=["admin-api"], dependencies=[Depends(require_admin_api_key)])


@router.get("/dashboard")
async def dashboard(hub: ServiceHub = Depends(get_hub)) -> dict:
    billing = getattr(hub, "billing", None)
    if billing is not None:
        try:
            await asyncio.wait_for(billing.snapshot_traffic(), timeout=8)
        except TimeoutError:
            pass
    return await hub.dashboard.overview()


@router.get("/users")
async def users(search: str | None = None, hub: ServiceHub = Depends(get_hub)) -> list[dict]:
    records = await hub.accounts.list_users(search)
    return [
        {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "username": user.username,
            "first_name": user.first_name,
            "status": user.status,
            "balance_rub": str(user.balance_rub),
        }
        for user in records
    ]


@router.get("/users/{user_id}")
async def user_detail(user_id: str, hub: ServiceHub = Depends(get_hub)) -> dict:
    card = await hub.accounts.user_card(user_id)
    subscription = card["subscription"]
    return {
        "user": {
            "id": card["user"].id,
            "telegram_id": card["user"].telegram_id,
            "username": card["user"].username,
            "status": card["user"].status,
            "balance_rub": str(card["user"].balance_rub),
        },
        "subscription": {
            "status": subscription.status if subscription else None,
            "plan": subscription.plan.name if subscription and subscription.plan else None,
            "next_billing_at": subscription.next_billing_at if subscription else None,
            "traffic_used_bytes": subscription.traffic_used_bytes if subscription else None,
        },
    }


@router.get("/topups")
async def topups(hub: ServiceHub = Depends(get_hub)) -> list[dict]:
    items = await hub.topups.list_requests()
    return [
        {
            "id": item.id,
            "user_id": item.user_id,
            "amount_rub": str(item.amount_rub),
            "status": item.status,
            "created_at": item.created_at,
        }
        for item in items
    ]


@router.post("/topups/{request_id}/approve")
async def approve_topup(request_id: str, hub: ServiceHub = Depends(get_hub)) -> dict:
    item = await hub.topups.approve(request_id, admin_id=None)
    return {"status": item.status, "id": item.id}


@router.post("/topups/{request_id}/reject")
async def reject_topup(request_id: str, hub: ServiceHub = Depends(get_hub)) -> dict:
    item = await hub.topups.reject(request_id, admin_id=None)
    return {"status": item.status, "id": item.id}


@router.post("/servers/sync")
async def sync_servers(hub: ServiceHub = Depends(get_hub)) -> dict:
    servers = await hub.catalog.sync_servers()
    return {"count": len(servers)}


@router.get("/online")
async def online(refresh: bool = False, hub: ServiceHub = Depends(get_hub)) -> list[dict]:
    if refresh:
        await hub.online.refresh_online_cache(detailed=True)
    records = await hub.online.list_online(only_online=False)
    return [
        {
            "user_id": item.user_id,
            "server_id": item.server_id,
            "remote_ip": item.remote_ip,
            "user_agent": item.user_agent,
            "last_activity_at": item.last_activity_at,
            "is_online": item.is_online,
        }
        for item in records
    ]
