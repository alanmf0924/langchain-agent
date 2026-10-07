from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from app.database import database_url


@dataclass(frozen=True, slots=True)
class CustomerAuthSettings:
    environment: str
    database_url: str
    jwt_secret: str
    cookie_secure: bool
    email_delivery_enabled: bool
    mail_mode: str
    mail_from: str
    mail_host: str
    mail_port: int
    mail_username: str
    mail_password: str
    mail_use_tls: bool
    mail_outbox_dir: str
    app_base_url: str
    access_token_ttl_seconds: int = 900
    # 长会话只用于安全地换取短 access token；当前产品约定为 7 天。
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 7
    verification_token_ttl_seconds: int = 60 * 60 * 24
    password_reset_token_ttl_seconds: int = 60 * 30


@lru_cache(maxsize=1)
def get_customer_auth_settings() -> CustomerAuthSettings:
    environment = os.getenv("APP_ENV", "development")
    default_secret = "local-customer-development-secret-must-not-reach-production"
    jwt_secret = os.getenv("CUSTOMER_JWT_SECRET", default_secret)
    if environment == "production" and jwt_secret == default_secret:
        raise RuntimeError("CUSTOMER_JWT_SECRET must be configured in production")
    mail_mode = os.getenv("CUSTOMER_MAIL_MODE", "log").lower()
    if mail_mode not in {"log", "file", "smtp"}:
        raise RuntimeError("CUSTOMER_MAIL_MODE must be log, file or smtp")
    mail_host = os.getenv("CUSTOMER_MAIL_HOST", "")
    mail_username = os.getenv("CUSTOMER_MAIL_USERNAME", "")
    mail_password = os.getenv("CUSTOMER_MAIL_PASSWORD", "")
    if mail_mode == "smtp" and (not mail_host or not mail_username or not mail_password):
        raise RuntimeError("SMTP mail mode requires CUSTOMER_MAIL_HOST, USERNAME and PASSWORD")
    cookie_secure = os.getenv("CUSTOMER_COOKIE_SECURE", "false").lower() == "true"
    app_base_url = os.getenv("CUSTOMER_APP_BASE_URL", "http://localhost:5179").rstrip("/")
    if environment == "production" and mail_mode != "smtp":
        raise RuntimeError("CUSTOMER_MAIL_MODE=smtp is required in production")
    if environment == "production" and not cookie_secure:
        raise RuntimeError("CUSTOMER_COOKIE_SECURE=true is required in production")
    if environment == "production" and not app_base_url.startswith("https://"):
        raise RuntimeError("CUSTOMER_APP_BASE_URL must use HTTPS in production")
    shared_database_url = database_url()
    if shared_database_url.startswith("sqlite+pysqlite://"):
        shared_database_url = shared_database_url.replace(
            "sqlite+pysqlite://", "sqlite+aiosqlite://", 1
        )
    return CustomerAuthSettings(
        environment=environment,
        database_url=shared_database_url,
        jwt_secret=jwt_secret,
        cookie_secure=cookie_secure,
        email_delivery_enabled=os.getenv("CUSTOMER_EMAIL_DELIVERY_ENABLED", "false").lower()
        == "true",
        mail_mode=mail_mode,
        mail_from=os.getenv("CUSTOMER_MAIL_FROM", "no-reply@example.test"),
        mail_host=mail_host,
        mail_port=int(os.getenv("CUSTOMER_MAIL_PORT", "465")),
        mail_username=mail_username,
        mail_password=mail_password,
        mail_use_tls=os.getenv("CUSTOMER_MAIL_USE_TLS", "true").lower() == "true",
        mail_outbox_dir=os.getenv("CUSTOMER_MAIL_OUTBOX_DIR", "/tmp/skin-assistant-mail-outbox"),
        app_base_url=app_base_url,
    )
