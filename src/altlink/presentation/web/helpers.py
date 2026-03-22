from __future__ import annotations

import secrets
from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException, Request, status
from starlette.responses import RedirectResponse

from altlink.core.security import generate_csrf_token, verify_csrf_token


def ensure_session_id(request: Request) -> str:
    session_id = request.session.get("session_id")
    if not session_id:
        session_id = secrets.token_urlsafe(24)
        request.session["session_id"] = session_id
    return session_id


def build_csrf_token(request: Request) -> str:
    session_id = ensure_session_id(request)
    return generate_csrf_token(request.app.state.settings.secret_key, session_id)


def verify_csrf(request: Request, token: str | None) -> None:
    session_id = ensure_session_id(request)
    if not verify_csrf_token(request.app.state.settings.secret_key, session_id, token):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неверный CSRF token")


def admin_redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def format_money(value: Decimal | None) -> str:
    if value is None:
        return "0.00 ₽"
    return f"{value:.2f} ₽"


def format_bytes(value: int | None) -> str:
    if not value:
        return "0 Б"
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    size = float(value)
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    return f"{size:.2f} {units[unit]}"


def format_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone().strftime("%d.%m.%Y %H:%M")

