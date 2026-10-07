from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from app.database import database_url


@dataclass(frozen=True, slots=True)
class OaSettings:
    environment: str
    database_url: str
    jwt_secret: str
    cors_origins: tuple[str, ...]
    cookie_secure: bool
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 14


@lru_cache(maxsize=1)
def get_oa_settings() -> OaSettings:
    environment = os.getenv("APP_ENV", "development")
    default_secret = "local-development-only-secret-must-not-reach-production"
    jwt_secret = os.getenv("OA_JWT_SECRET", default_secret)
    if environment == "production" and jwt_secret == default_secret:
        raise RuntimeError("OA_JWT_SECRET must be configured in production")
    origins = tuple(
        item.strip()
        for item in os.getenv("OA_CORS_ORIGINS", "http://localhost:5173").split(",")
        if item.strip()
    )
    shared_database_url = database_url()
    if shared_database_url.startswith("sqlite+pysqlite://"):
        # 同一内存库在单测中分别由同步助手和异步 OA Repository 访问。
        shared_database_url = shared_database_url.replace(
            "sqlite+pysqlite://", "sqlite+aiosqlite://", 1
        )
    return OaSettings(
        environment=environment,
        database_url=shared_database_url,
        jwt_secret=jwt_secret,
        cors_origins=origins,
        cookie_secure=os.getenv("OA_COOKIE_SECURE", "false").lower() == "true",
    )
