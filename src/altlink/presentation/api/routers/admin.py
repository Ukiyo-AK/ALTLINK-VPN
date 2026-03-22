from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from altlink.application.services import BillingService, DashboardService, ServerService
from altlink.presentation.api.deps import get_admin_user, get_remnawave, get_session, get_settings

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/dashboard")
async def dashboard(
    _admin=Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    service = DashboardService(session, settings, remnawave)
    data = await service.get_dashboard()
    return {
        "counts": data["counts"],
        "debts_rub": str(data["debts_rub"]),
        "total_traffic_bytes": data["total_traffic_bytes"],
        "servers": [
            {
                "id": server.id,
                "name": server.name,
                "load_percent": float(server.load_percent),
                "current_clients_count": server.current_clients_count,
                "max_clients_count": server.max_clients_count,
                "is_connected": server.is_connected,
            }
            for server in data["servers"]
        ],
    }


@router.post("/servers/sync")
async def sync_servers(
    _admin=Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    service = ServerService(session, settings, remnawave)
    servers = await service.sync_from_remnawave()
    return {"synced": len(servers)}


@router.get("/topups")
async def list_topups(
    _admin=Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    remnawave=Depends(get_remnawave),
):
    from sqlalchemy import desc, select
    from altlink.infrastructure.db.models import TopupRequest

    rows = (await session.execute(select(TopupRequest).order_by(desc(TopupRequest.created_at)))).scalars().all()
    return {
        "items": [
            {
                "id": row.id,
                "user_id": row.user_id,
                "amount_rub": str(row.amount_rub),
                "status": row.status.value,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }
