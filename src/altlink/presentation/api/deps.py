from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from altlink.application.services import AdminAuthService
from altlink.infrastructure.db.models import AdminUser
from altlink.settings import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_remnawave(request: Request):
    return request.app.state.remnawave


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_admin_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AdminUser:
    admin_id = request.session.get("admin_user_id")
    if not admin_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется авторизация")
    admin = await session.get(AdminUser, admin_id)
    if admin is None or not admin.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Сессия недействительна")
    return admin


async def ensure_admin_bot_access(
    telegram_id: int,
    request: Request,
    session: AsyncSession,
) -> bool:
    service = AdminAuthService(session, request.app.state.settings, request.app.state.remnawave)
    return await service.is_admin_telegram_id(telegram_id)

