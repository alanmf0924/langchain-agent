from __future__ import annotations

import hashlib
import secrets
import time
from collections import deque
from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

from app.customer.config import get_customer_auth_settings

password_hasher = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def create_access_token(customer_id: str, token_version: int) -> str:
    settings = get_customer_auth_settings()
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": customer_id,
            "tv": token_version,
            "iat": now,
            "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
            "typ": "customer_access",
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_access_token(token: str) -> dict[str, object]:
    try:
        payload = jwt.decode(token, get_customer_auth_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as error:
        raise ValueError("invalid customer access token") from error
    if payload.get("typ") != "customer_access" or not isinstance(payload.get("sub"), str):
        raise ValueError("invalid customer access token")
    return payload


def create_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def hash_opaque_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LoginAttemptLimiter:
    """单实例补充限流，正式部署仍必须在网关执行 IP/账号级防撞库限流。"""

    def __init__(
        self, max_failures: int = 5, window_seconds: int = 600, max_keys: int = 10_000
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self.attempts: dict[str, deque[float]] = {}

    def is_blocked(self, key: str) -> bool:
        attempts = self._active_attempts(key)
        return len(attempts) >= self.max_failures

    def record_failure(self, key: str) -> None:
        attempts = self._active_attempts(key)
        if len(self.attempts) >= self.max_keys and key not in self.attempts:
            self.attempts.pop(next(iter(self.attempts)))
        attempts.append(time.monotonic())
        self.attempts[key] = attempts

    def clear(self, key: str) -> None:
        self.attempts.pop(key, None)

    def _active_attempts(self, key: str) -> deque[float]:
        now = time.monotonic()
        attempts = self.attempts.get(key, deque())
        while attempts and attempts[0] <= now - self.window_seconds:
            attempts.popleft()
        if not attempts:
            self.attempts.pop(key, None)
        return attempts
