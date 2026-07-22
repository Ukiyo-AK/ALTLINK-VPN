from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from altlink.domain.enums import SystemEventLevel
from altlink.infrastructure.db.models import SystemEvent
from altlink.infrastructure.remnawave_client import RemnawaveGateway
from altlink.settings import Settings


class ServiceError(RuntimeError):
    pass


class NotFoundError(ServiceError):
    pass


class ConflictError(ServiceError):
    pass


class AuthError(ServiceError):
    pass


class BaseService:
    source = "service"

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        remnawave: RemnawaveGateway | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.remnawave = remnawave

    async def log_event(
        self,
        *,
        level: SystemEventLevel,
        event_type: str,
        message: str,
        payload: dict | None = None,
        actor_admin_id: str | None = None,
        subject_user_id: str | None = None,
    ) -> None:
        if subject_user_id is None and isinstance(payload, dict):
            payload_user_id = payload.get("user_id")
            if payload_user_id:
                subject_user_id = str(payload_user_id)
        self.session.add(
            SystemEvent(
                subject_user_id=subject_user_id,
                actor_admin_id=actor_admin_id,
                level=level,
                source=self.source,
                event_type=event_type,
                message=message,
                payload=payload,
            )
        )
