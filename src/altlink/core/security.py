from __future__ import annotations

import hmac
import secrets
from hashlib import sha256

from passlib.context import CryptContext


pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def generate_csrf_token(secret_key: str, session_id: str) -> str:
    nonce = secrets.token_hex(16)
    signature = hmac.new(secret_key.encode(), f"{session_id}:{nonce}".encode(), sha256).hexdigest()
    return f"{nonce}:{signature}"


def verify_csrf_token(secret_key: str, session_id: str, token: str | None) -> bool:
    if not token or ":" not in token:
        return False
    nonce, signature = token.split(":", 1)
    expected = hmac.new(secret_key.encode(), f"{session_id}:{nonce}".encode(), sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
