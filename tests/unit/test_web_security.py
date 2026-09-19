import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from altlink.presentation.web.routes import parse_gb_to_bytes, parse_int_query, validate_csrf
from altlink.utils.telegram_web import verify_telegram_auth_payload, verify_telegram_webapp_init_data
from altlink.presentation.api.dependencies import require_admin_api_key
from altlink.db import create_engine
from altlink.settings import Settings


TOKEN = "123456:test-token-not-used-for-network"


def signed_payload(*, webapp, age=0, user_id=123456):
    data = {"auth_date": str(int(time.time()) - age)}
    if webapp:
        data["user"] = json.dumps({"id": user_id, "first_name": "Test"})
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    else:
        data.update(id=str(user_id), first_name="Test")
        secret = hashlib.sha256(TOKEN.encode()).digest()
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return data


def verify(data, webapp):
    if webapp:
        return verify_telegram_webapp_init_data(urlencode(data), bot_token=TOKEN, max_age_seconds=300)
    return verify_telegram_auth_payload(data, bot_token=TOKEN, max_age_seconds=300)


@pytest.mark.parametrize("webapp", [False, True])
@pytest.mark.parametrize("age,accepted", [(0, True), (120, True), (-10, True), (360, False), (-120, False)])
def test_telegram_auth_signature_and_age(webapp, age, accepted):
    assert bool(verify(signed_payload(webapp=webapp, age=age), webapp)) is accepted


@pytest.mark.parametrize("webapp", [False, True])
@pytest.mark.parametrize("bad_hash", [None, "", "x" * 64, "\u044f" * 64])
def test_telegram_auth_rejects_malformed_hash(webapp, bad_hash):
    data = signed_payload(webapp=webapp)
    data["hash"] = bad_hash
    assert not verify(data, webapp)


@pytest.mark.parametrize("webapp", [False, True])
def test_telegram_auth_rejects_tampered_identity(webapp):
    data = signed_payload(webapp=webapp)
    data["user" if webapp else "id"] = json.dumps({"id": 999}) if webapp else "999"
    assert not verify(data, webapp)


def test_webapp_rejects_ambiguous_duplicate_parameters():
    raw = urlencode(signed_payload(webapp=True))
    assert verify_telegram_webapp_init_data(raw + "&user=%7B%22id%22%3A999%7D", bot_token=TOKEN, max_age_seconds=300) is None


@pytest.mark.parametrize("user_id", [True, 0, -1, "123", 2**63])
def test_webapp_rejects_invalid_user_identifier(user_id):
    assert not verify(signed_payload(webapp=True, user_id=user_id), True)


@pytest.mark.parametrize("expected,supplied", [(None, None), ("", ""), ("a", None), ("a", "b"), ("a", "\u044f"), ("a", ["a"])])
def test_csrf_rejects_missing_or_invalid_tokens(expected, supplied):
    request = SimpleNamespace(session={"csrf_token": expected})
    with pytest.raises(HTTPException) as error:
        validate_csrf(request, {"csrf_token": supplied})
    assert error.value.status_code == 400


def test_csrf_accepts_matching_nonempty_token():
    validate_csrf(SimpleNamespace(session={"csrf_token": "secret"}), {"csrf_token": "secret"})


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e99999", str(2**63), "1000000000000000000"])
def test_traffic_filters_reject_values_outside_database_range(value):
    assert parse_gb_to_bytes(value) is None


def test_integer_filters_stay_in_database_range():
    assert parse_int_query(str(2**63)) is None
    assert parse_int_query("12") == 12
    assert parse_gb_to_bytes("1.5") == 1610612736


@pytest.mark.asyncio
@pytest.mark.parametrize("expected,supplied,code", [("", "", 503), ("change-me-admin-api-key", "change-me-admin-api-key", 503), ("real-key", None, 401), ("real-key", "wrong", 401), ("real-key", "\u044f", 401)])
async def test_admin_api_rejects_default_or_wrong_key(expected, supplied, code):
    container = SimpleNamespace(settings=SimpleNamespace(admin_api_key=expected))
    with pytest.raises(HTTPException) as error:
        await require_admin_api_key(supplied, container)
    assert error.value.status_code == code


@pytest.mark.asyncio
async def test_admin_api_accepts_configured_key():
    await require_admin_api_key("real-key", SimpleNamespace(settings=SimpleNamespace(admin_api_key="real-key")))


@pytest.mark.asyncio
async def test_sql_errors_hide_sensitive_parameters():
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    engine = create_engine(Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:"))
    try:
        async with engine.begin() as connection:
            with pytest.raises(SQLAlchemyError) as error:
                await connection.execute(text("INSERT INTO missing_table (secret) VALUES (:secret)"), {"secret": "private-key-must-not-appear"})
        assert "private-key-must-not-appear" not in str(error.value)
        assert "parameters hidden" in str(error.value)
    finally:
        await engine.dispose()
