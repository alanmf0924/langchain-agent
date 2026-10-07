from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

from app.oa.config import get_oa_settings

password_hasher = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def create_access_token(user_id: str, token_version: int) -> str:
    settings = get_oa_settings()
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user_id,
            "tv": token_version,
            "iat": now,
            "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
            "typ": "access",
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_access_token(token: str) -> dict[str, object]:
    try:
        payload = jwt.decode(token, get_oa_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as error:
        raise ValueError("invalid access token") from error
    if payload.get("typ") != "access" or not isinstance(payload.get("sub"), str):
        raise ValueError("invalid access token")
    return payload


def create_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def hash_opaque_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
