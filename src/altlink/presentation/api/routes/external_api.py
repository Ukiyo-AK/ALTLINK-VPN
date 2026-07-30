from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from altlink.application.services.base import NotFoundError
from altlink.application.services.registry import ServiceHub
from altlink.domain.enums import PlanCode
from altlink.domain.external_api import EXTERNAL_API_USER_SCOPES
from altlink.infrastructure.db.models import Subscription, User
from altlink.presentation.api.dependencies import (
    ExternalApiPrincipal,
    get_hub,
    require_external_api_client,
)
from altlink.utils.time import ensure_utc

router = APIRouter(
    prefix="/api/external/v1",
    tags=["external-api"],
)


class ExternalApiClientResponse(BaseModel):
    id: str
    name: str
    scopes: list[str]
    expires_at: datetime | None = None


class ExternalPlanResponse(BaseModel):
    code: str
    name: str
    is_trial: bool
    period_days: int


class ExternalSubscriptionResponse(BaseModel):
    id: str
    status: str
    started_at: datetime
    ends_at: datetime
    next_billing_at: datetime
    auto_renew: bool
    device_limit: int | None = None


class ExternalTrafficResponse(BaseModel):
    used_bytes: int
    whitelist_used_bytes: int
    whitelist_billed_bytes: int
    limit_bytes: int | None = None
    last_reset_at: datetime | None = None


class ExternalDevicesResponse(BaseModel):
    count: int
    checked_at: datetime | None = None


class ExternalProfileResponse(BaseModel):
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None
    registered_at: datetime | None = None
    last_seen_at: datetime | None = None


class ExternalReferralResponse(BaseModel):
    code: str | None = None
    reward_granted_at: datetime | None = None


class ExternalUserResponse(BaseModel):
    id: str
    updated_at: datetime
    telegram_id: int | None = None
    status: str | None = None
    access_active: bool | None = None
    plan: ExternalPlanResponse | None = None
    profile: ExternalProfileResponse | None = None
    balance_rub: str | None = None
    subscription: ExternalSubscriptionResponse | None = None
    traffic: ExternalTrafficResponse | None = None
    devices: ExternalDevicesResponse | None = None
    referral: ExternalReferralResponse | None = None


class ExternalUserListMeta(BaseModel):
    limit: int
    offset: int
    count: int
    has_more: bool
    next_offset: int | None = None
    active_only: bool
    granted_fields: list[str]


class ExternalUserListResponse(BaseModel):
    items: list[ExternalUserResponse]
    meta: ExternalUserListMeta


def enum_value(value) -> str:
    return str(getattr(value, "value", value))


def utc_datetime(value: datetime | None) -> datetime | None:
    return ensure_utc(value) if value is not None else None


def serialize_external_user(
    user: User,
    subscription: Subscription | None,
    scopes: frozenset[str],
) -> dict:
    item: dict[str, object] = {
        "id": user.id,
        "updated_at": utc_datetime(user.updated_at),
    }
    if "users.telegram_id" in scopes:
        item["telegram_id"] = user.telegram_id
    if "users.status" in scopes:
        item["status"] = enum_value(user.status)
        item["access_active"] = subscription is not None
    if "users.plan" in scopes:
        plan = subscription.plan if subscription is not None else None
        item["plan"] = (
            {
                "code": enum_value(plan.code),
                "name": plan.name,
                "is_trial": bool(plan.is_trial),
                "period_days": plan.period_days,
            }
            if plan is not None
            else None
        )
    if "users.profile" in scopes:
        item["profile"] = {
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "language_code": user.language_code,
            "registered_at": utc_datetime(user.registration_completed_at or user.created_at),
            "last_seen_at": utc_datetime(user.last_seen_at),
        }
    if "users.balance" in scopes:
        item["balance_rub"] = f"{Decimal(user.balance_rub):.2f}"
    if "users.subscription" in scopes:
        plan = subscription.plan if subscription is not None else None
        item["subscription"] = (
            {
                "id": subscription.id,
                "status": enum_value(subscription.status),
                "started_at": utc_datetime(subscription.started_at),
                "ends_at": utc_datetime(subscription.ends_at),
                "next_billing_at": utc_datetime(subscription.next_billing_at),
                "auto_renew": bool(subscription.auto_renew),
                "device_limit": plan.device_limit if plan is not None else None,
            }
            if subscription is not None
            else None
        )
    if "users.traffic" in scopes:
        item["traffic"] = (
            {
                "used_bytes": int(subscription.traffic_used_bytes or 0),
                "whitelist_used_bytes": int(subscription.whitelist_traffic_used_bytes or 0),
                "whitelist_billed_bytes": int(subscription.whitelist_traffic_billed_bytes or 0),
                "limit_bytes": subscription.traffic_limit_bytes,
                "last_reset_at": utc_datetime(subscription.last_traffic_reset_at),
            }
            if subscription is not None
            else None
        )
    if "users.devices" in scopes:
        item["devices"] = {
            "count": int(user.hwid_device_count or 0),
            "checked_at": utc_datetime(user.hwid_devices_checked_at),
        }
    if "users.referrals" in scopes:
        item["referral"] = {
            "code": user.referral_code,
            "reward_granted_at": utc_datetime(user.referral_reward_granted_at),
        }
    return item


def ensure_user_scope(principal: ExternalApiPrincipal) -> None:
    if not principal.scopes.intersection(EXTERNAL_API_USER_SCOPES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="У API-клиента нет разрешений на данные пользователей.",
        )


@router.get(
    "/client",
    response_model=ExternalApiClientResponse,
    summary="Проверить API-ключ",
    description="Возвращает API-клиента и выданные ему разрешения. Секрет ключа не возвращается.",
)
async def external_api_client(
    principal: ExternalApiPrincipal = Depends(require_external_api_client),
) -> ExternalApiClientResponse:
    return ExternalApiClientResponse(
        id=principal.client_id,
        name=principal.name,
        scopes=sorted(principal.scopes),
        expires_at=utc_datetime(principal.expires_at),
    )


@router.get(
    "/users",
    response_model=ExternalUserListResponse,
    response_model_exclude_none=True,
    summary="Получить пользователей",
    description=(
        "По умолчанию возвращает только пользователей с действующим доступом. "
        "Каждое поле включается только при наличии соответствующего scope."
    ),
)
async def external_users(
    active_only: bool = Query(
        default=True,
        description="Оставить только пользователей с действующей подпиской или тестовым периодом.",
    ),
    plan_code: PlanCode | None = Query(default=None, description="Фильтр по текущему тарифу."),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: ExternalApiPrincipal = Depends(require_external_api_client),
    hub: ServiceHub = Depends(get_hub),
) -> ExternalUserListResponse:
    ensure_user_scope(principal)
    page = await hub.external_api.list_users_for_api(
        active_only=active_only,
        plan_code=plan_code,
        limit=limit,
        offset=offset,
    )
    items = [
        serialize_external_user(
            user,
            page.subscriptions_by_user_id.get(user.id),
            principal.scopes,
        )
        for user in page.users
    ]
    return ExternalUserListResponse(
        items=[ExternalUserResponse.model_validate(item) for item in items],
        meta=ExternalUserListMeta(
            limit=limit,
            offset=offset,
            count=len(items),
            has_more=page.has_more,
            next_offset=offset + len(items) if page.has_more else None,
            active_only=active_only,
            granted_fields=sorted(principal.scopes.intersection(EXTERNAL_API_USER_SCOPES)),
        ),
    )


@router.get(
    "/users/by-telegram/{telegram_id}",
    response_model=ExternalUserResponse,
    response_model_exclude_none=True,
    summary="Найти пользователя по Telegram ID",
    description=(
        "Удобный endpoint для проверки права пользователя на внешний сервис. "
        "Требует scope users.telegram_id."
    ),
)
async def external_user_by_telegram_id(
    telegram_id: int,
    principal: ExternalApiPrincipal = Depends(require_external_api_client),
    hub: ServiceHub = Depends(get_hub),
) -> ExternalUserResponse:
    if not principal.has_scope("users.telegram_id"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Для поиска по Telegram ID требуется разрешение users.telegram_id.",
        )
    try:
        user, subscription = await hub.external_api.get_user_by_telegram_id_for_api(
            telegram_id
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ExternalUserResponse.model_validate(
        serialize_external_user(user, subscription, principal.scopes)
    )


@router.get(
    "/users/{user_id}",
    response_model=ExternalUserResponse,
    response_model_exclude_none=True,
    summary="Получить пользователя",
    description="Возвращает разрешённые конкретному API-клиенту поля пользователя.",
)
async def external_user_detail(
    user_id: str,
    principal: ExternalApiPrincipal = Depends(require_external_api_client),
    hub: ServiceHub = Depends(get_hub),
) -> ExternalUserResponse:
    ensure_user_scope(principal)
    try:
        user, subscription = await hub.external_api.get_user_for_api(user_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ExternalUserResponse.model_validate(
        serialize_external_user(user, subscription, principal.scopes)
    )
