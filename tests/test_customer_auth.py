from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.customer import router as customer_router
from app.customer.config import get_customer_auth_settings
from app.customer.database import SessionLocal
from app.customer.database import engine as customer_engine
from app.customer.mailer import send_account_email
from app.customer.models import CustomerBase, CustomerEmailDeliveryRequest
from app.main import app


async def reset_customer_identity() -> None:
    async with customer_engine.begin() as connection:
        await connection.run_sync(CustomerBase.metadata.drop_all)
        await connection.run_sync(CustomerBase.metadata.create_all)


def token_from_mail(body: str) -> str:
    link = next(line for line in body.splitlines() if line.startswith("http"))
    token = parse_qs(urlparse(link).query).get("token", [""])[0]
    assert token
    return token


def test_customer_can_register_login_and_record_password_recovery_before_email_delivery(
    monkeypatch,
) -> None:
    asyncio.run(reset_customer_identity())
    mails: list[str] = []

    async def capture_mail(_: str, __: str, body: str) -> None:
        mails.append(body)

    monkeypatch.delenv("CUSTOMER_EMAIL_DELIVERY_ENABLED", raising=False)
    get_customer_auth_settings.cache_clear()
    monkeypatch.setattr(customer_router, "send_account_email", capture_mail)
    try:
        with TestClient(app) as client:
            registration = client.post(
                "/api/customer/auth/register",
                json={
                    "username": "ming",
                    "email": "Ming@example.test",
                    "display_name": "小明",
                    "password": "CorrectHorseBattery1",
                },
            )
            assert registration.status_code == 202
            assert registration.json()["message"] == "注册成功，邮箱已记录。邮箱验证服务准备中，请使用账号和密码登录。"
            assert mails == []

            account = client.post(
                "/api/customer/auth/login",
                json={"username": "ming", "password": "CorrectHorseBattery1"},
            )
            assert account.status_code == 200
            assert account.json()["user"]["email"] == "ming@example.test"
            assert account.json()["user"]["email_verified"] is False
            assert "Max-Age=604800" in account.headers["set-cookie"]
            assert get_customer_auth_settings().refresh_token_ttl_seconds == 60 * 60 * 24 * 7

            missing = client.post(
                "/api/customer/auth/password-reset-requests",
                json={"email": "missing@example.test"},
            )
            requested = client.post(
                "/api/customer/auth/password-reset-requests",
                json={"email": "ming@example.test"},
            )
            assert missing.status_code == requested.status_code == 202
            assert missing.json() == requested.json()
            assert "已记录" in requested.json()["message"]
    finally:
        get_customer_auth_settings.cache_clear()

    async def recovery_request_count() -> int:
        async with SessionLocal() as session:
            records = await session.scalars(select(CustomerEmailDeliveryRequest))
            return len(list(records))

    assert asyncio.run(recovery_request_count()) == 2


def test_customer_can_use_an_email_address_as_the_username(monkeypatch) -> None:
    asyncio.run(reset_customer_identity())
    monkeypatch.delenv("CUSTOMER_EMAIL_DELIVERY_ENABLED", raising=False)
    get_customer_auth_settings.cache_clear()
    try:
        with TestClient(app) as client:
            registration = client.post(
                "/api/customer/auth/register",
                json={
                    "username": "customer@example.test",
                    "email": "customer@example.test",
                    "display_name": "邮箱账号用户",
                    "password": "CorrectHorseBattery1",
                },
            )
            assert registration.status_code == 202
            login = client.post(
                "/api/customer/auth/login",
                json={"username": "customer@example.test", "password": "CorrectHorseBattery1"},
            )
            assert login.status_code == 200
            assert login.json()["user"]["username"] == "customer@example.test"
    finally:
        get_customer_auth_settings.cache_clear()


def test_email_delivery_can_enable_verification_and_reset_flow(monkeypatch) -> None:
    asyncio.run(reset_customer_identity())
    mails: list[str] = []

    async def capture_mail(_: str, __: str, body: str) -> None:
        mails.append(body)

    monkeypatch.setenv("CUSTOMER_EMAIL_DELIVERY_ENABLED", "true")
    get_customer_auth_settings.cache_clear()
    monkeypatch.setattr(customer_router, "send_account_email", capture_mail)
    try:
        with TestClient(app) as client:
            registration = client.post(
                "/api/customer/auth/register",
                json={
                    "username": "ming",
                    "email": "Ming@example.test",
                    "display_name": "小明",
                    "password": "CorrectHorseBattery1",
                },
            )
            assert registration.status_code == 202
            assert len(mails) == 1

            # 启用真实投递后，验证仍是登录前置条件。
            assert (
                client.post(
                    "/api/customer/auth/login",
                    json={"username": "ming", "password": "CorrectHorseBattery1"},
                ).status_code
                == 401
            )

            verification = client.post(
                "/api/customer/auth/email-verifications/confirm",
                json={"token": token_from_mail(mails[-1])},
            )
            assert verification.status_code == 204
            account = client.post(
                "/api/customer/auth/login",
                json={"username": "ming", "password": "CorrectHorseBattery1"},
            )
            assert account.status_code == 200
            old_headers = {"Authorization": f"Bearer {account.json()['access_token']}"}

            changed = client.put(
                "/api/customer/auth/password",
                json={
                    "current_password": "CorrectHorseBattery1",
                    "new_password": "AnotherCorrectBattery2",
                },
                headers=old_headers,
            )
            assert changed.status_code == 204
            assert client.get("/api/customer/auth/me", headers=old_headers).status_code == 401
            after_change = client.post(
                "/api/customer/auth/login",
                json={"username": "ming", "password": "AnotherCorrectBattery2"},
            )
            assert after_change.status_code == 200
            changed_headers = {"Authorization": f"Bearer {after_change.json()['access_token']}"}

            missing = client.post(
                "/api/customer/auth/password-reset-requests",
                json={"email": "missing@example.test"},
            )
            requested = client.post(
                "/api/customer/auth/password-reset-requests",
                json={"email": "ming@example.test"},
            )
            assert missing.status_code == requested.status_code == 202
            assert missing.json() == requested.json()
            reset = client.post(
                "/api/customer/auth/password-resets",
                json={
                    "token": token_from_mail(mails[-1]),
                    "password": "FinalCorrectBattery3",
                },
            )
            assert reset.status_code == 204
            assert client.get("/api/customer/auth/me", headers=changed_headers).status_code == 401
            assert (
                client.post(
                    "/api/customer/auth/password-resets",
                    json={
                        "token": token_from_mail(mails[-1]),
                        "password": "FinalCorrectBattery3",
                    },
                ).status_code
                == 400
            )
            assert (
                client.post(
                    "/api/customer/auth/login",
                    json={"username": "ming", "password": "FinalCorrectBattery3"},
                ).status_code
                == 200
            )
    finally:
        get_customer_auth_settings.cache_clear()


def test_development_file_mail_delivery_keeps_reset_link_out_of_logs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CUSTOMER_MAIL_MODE", "file")
    monkeypatch.setenv("CUSTOMER_MAIL_OUTBOX_DIR", str(tmp_path))
    get_customer_auth_settings.cache_clear()
    try:
        asyncio.run(
            send_account_email(
                "customer@example.test",
                "重置密码",
                "https://localhost/reset-password?token=development-only-token",
            )
        )
        messages = list(tmp_path.glob("customer-account-*.eml"))
        assert len(messages) == 1
        assert b"development-only-token" in messages[0].read_bytes()
    finally:
        get_customer_auth_settings.cache_clear()


def test_production_refuses_a_non_smtp_customer_mail_mode(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CUSTOMER_JWT_SECRET", "a-high-entropy-test-secret")
    monkeypatch.setenv("CUSTOMER_MAIL_MODE", "log")
    get_customer_auth_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="CUSTOMER_MAIL_MODE=smtp"):
            get_customer_auth_settings()
    finally:
        get_customer_auth_settings.cache_clear()
