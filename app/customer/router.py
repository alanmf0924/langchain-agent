from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.customer.config import get_customer_auth_settings
from app.customer.database import get_session
from app.customer.dependencies import get_current_customer
from app.customer.mailer import send_account_email
from app.customer.models import (
    Customer,
    CustomerEmailDeliveryRequest,
    CustomerIdentityToken,
    CustomerRefreshSession,
    new_customer_id,
)
from app.customer.security import (
    LoginAttemptLimiter,
    create_access_token,
    create_opaque_token,
    hash_opaque_token,
    hash_password,
    verify_password,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/customer", tags=["前台用户鉴权"])
login_attempt_limiter = LoginAttemptLimiter()
EMAIL_VERIFICATION_PURPOSE = "email_verification"
PASSWORD_RESET_PURPOSE = "password_reset"


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    local, separator, domain = email.partition("@")
    if not separator or not local or not domain or "." not in domain or len(email) > 254:
        raise ValueError("请输入有效的邮箱地址")
    return email


def normalize_display_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("昵称不能为空")
    return name


def validate_password(value: str) -> str:
    if (
        len(value) < 12
        or not any(char.islower() for char in value)
        or not any(char.isupper() for char in value)
        or not any(char.isdigit() for char in value)
    ):
        raise ValueError("密码至少 12 位，且须包含大小写字母与数字")
    return value


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    email: str = Field(min_length=5, max_length=254)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=128)

    _normalize_email = field_validator("email")(normalize_email)
    _normalize_display_name = field_validator("display_name")(normalize_display_name)
    _validate_password = field_validator("password")(validate_password)


class EmailRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)

    _normalize_email = field_validator("email")(normalize_email)


class TokenRequest(BaseModel):
    token: str = Field(min_length=32, max_length=160)


class PasswordResetRequest(TokenRequest):
    password: str = Field(min_length=12, max_length=128)

    _validate_password = field_validator("password")(validate_password)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)

    _validate_password = field_validator("new_password")(validate_password)


class MessageResponse(BaseModel):
    message: str


class CustomerResponse(BaseModel):
    id: str
    username: str
    display_name: str
    email: str
    email_verified: bool


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    user: CustomerResponse


def customer_response(customer: Customer) -> CustomerResponse:
    return CustomerResponse(
        id=customer.id,
        username=customer.username,
        display_name=customer.display_name,
        email=customer.email or "",
        email_verified=customer.email_verified,
    )


def attempt_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{username.lower()}"


def as_utc(value: datetime) -> datetime:
    """SQLite 测试库会丢失 timezone 信息；业务比较仍统一按 UTC 处理。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def email_digest(email: str) -> str:
    """给后续异步投递队列去重用，避免把未注册邮箱明文另存一份。"""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


async def record_email_delivery_request(
    session: AsyncSession, email: str, purpose: str, customer: Customer | None
) -> None:
    session.add(
        CustomerEmailDeliveryRequest(
            id=new_customer_id(),
            customer_id=customer.id if customer else None,
            purpose=purpose,
            recipient_hash=email_digest(email),
            delivery_status="pending_delivery",
        )
    )


async def issue_session(session: AsyncSession, customer: Customer) -> tuple[str, str, str]:
    settings = get_customer_auth_settings()
    refresh_token, csrf_token = create_opaque_token(), create_opaque_token()
    session.add(
        CustomerRefreshSession(
            id=new_customer_id(),
            customer_id=customer.id,
            token_hash=hash_opaque_token(refresh_token),
            csrf_hash=hash_opaque_token(csrf_token),
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
    )
    return create_access_token(customer.id, customer.token_version), refresh_token, csrf_token


async def revoke_customer_sessions(session: AsyncSession, customer_id: str) -> None:
    await session.execute(
        update(CustomerRefreshSession)
        .where(
            CustomerRefreshSession.customer_id == customer_id,
            CustomerRefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


async def issue_identity_token(
    session: AsyncSession, customer: Customer, purpose: str, ttl_seconds: int
) -> str:
    now = datetime.now(UTC)
    await session.execute(
        update(CustomerIdentityToken)
        .where(
            CustomerIdentityToken.customer_id == customer.id,
            CustomerIdentityToken.purpose == purpose,
            CustomerIdentityToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    token = create_opaque_token()
    session.add(
        CustomerIdentityToken(
            id=new_customer_id(),
            customer_id=customer.id,
            purpose=purpose,
            token_hash=hash_opaque_token(token),
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
    )
    return token


async def consume_identity_token(
    session: AsyncSession, token: str, purpose: str
) -> tuple[CustomerIdentityToken, Customer]:
    now = datetime.now(UTC)
    record = await session.scalar(
        select(CustomerIdentityToken).where(
            CustomerIdentityToken.token_hash == hash_opaque_token(token),
            CustomerIdentityToken.purpose == purpose,
        )
    )
    if record is None or record.consumed_at is not None or as_utc(record.expires_at) <= now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="链接无效或已过期")
    customer = await session.get(Customer, record.customer_id)
    if customer is None or not customer.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="链接无效或已过期")
    record.consumed_at = now
    return record, customer


def set_refresh_cookies(response: Response, refresh_token: str, csrf_token: str) -> None:
    settings = get_customer_auth_settings()
    options = {
        "max_age": settings.refresh_token_ttl_seconds,
        "secure": settings.cookie_secure,
        "samesite": "lax",
    }
    response.set_cookie(
        "customer_refresh_token", refresh_token, httponly=True, path="/api/customer/auth", **options
    )
    response.set_cookie("customer_csrf_token", csrf_token, httponly=False, path="/", **options)


def clear_refresh_cookies(response: Response) -> None:
    response.delete_cookie("customer_refresh_token", path="/api/customer/auth")
    response.delete_cookie("customer_csrf_token", path="/")


async def deliver_verification_email(customer: Customer, token: str) -> None:
    settings = get_customer_auth_settings()
    if not customer.email:
        return
    link = f"{settings.app_base_url}/verify-email?{urlencode({'token': token})}"
    await send_account_email(
        customer.email,
        "验证你的澄肌邮箱",
        f"请在 24 小时内打开以下链接验证邮箱：\n{link}\n\n如果不是你本人注册，请忽略此邮件。",
    )


async def deliver_password_reset_email(customer: Customer, token: str) -> None:
    settings = get_customer_auth_settings()
    if not customer.email:
        return
    link = f"{settings.app_base_url}/reset-password?{urlencode({'token': token})}"
    await send_account_email(
        customer.email,
        "重置你的澄肌密码",
        f"请在 30 分钟内打开以下链接设置新密码：\n{link}\n\n如果不是你本人发起，请忽略此邮件。",
    )


async def send_or_fail(delivery) -> None:
    try:
        await delivery
    except Exception as error:  # SMTP 异常不暴露给浏览器，也不能假称已经投递。
        logger.exception("Customer account email delivery failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="邮件暂未送达，请稍后重试",
        ) from error


@router.post(
    "/auth/register",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="注册前台用户",
)
async def register(
    payload: RegisterRequest, session: AsyncSession = Depends(get_session)
) -> MessageResponse:
    existing = await session.scalar(
        select(Customer).where(
            (Customer.username == payload.username) | (Customer.email == payload.email)
        )
    )
    if existing is not None:
        if existing.username == payload.username:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该账号已被使用")
        return MessageResponse(message="如果该邮箱可注册，我们已发送下一步说明")
    customer = Customer(
        id=new_customer_id(),
        username=payload.username,
        email=payload.email,
        email_verified=False,
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        password_changed_at=datetime.now(UTC),
    )
    session.add(customer)
    settings = get_customer_auth_settings()
    if settings.email_delivery_enabled:
        token = await issue_identity_token(
            session,
            customer,
            EMAIL_VERIFICATION_PURPOSE,
            settings.verification_token_ttl_seconds,
        )
    await session.commit()
    if settings.email_delivery_enabled:
        await send_or_fail(deliver_verification_email(customer, token))
        return MessageResponse(message="验证邮件已发送，请完成验证后登录")
    return MessageResponse(message="注册成功，邮箱已记录。邮箱验证服务准备中，请使用账号和密码登录。")


@router.post(
    "/auth/email-verifications",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="重新发送邮箱验证邮件",
)
async def resend_email_verification(
    payload: EmailRequest, session: AsyncSession = Depends(get_session)
) -> MessageResponse:
    customer = await session.scalar(select(Customer).where(Customer.email == payload.email))
    if customer is None or not customer.is_active or customer.email_verified:
        return MessageResponse(message="如果该邮箱需要验证，我们已发送下一步说明")
    if not get_customer_auth_settings().email_delivery_enabled:
        return MessageResponse(message="邮箱已记录，验证邮件服务准备中，暂不能发送验证链接。")
    token = await issue_identity_token(
        session,
        customer,
        EMAIL_VERIFICATION_PURPOSE,
        get_customer_auth_settings().verification_token_ttl_seconds,
    )
    await session.commit()
    await send_or_fail(deliver_verification_email(customer, token))
    return MessageResponse(message="如果该邮箱需要验证，我们已发送下一步说明")


@router.post(
    "/auth/email-verifications/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="确认邮箱验证链接",
)
async def confirm_email_verification(
    payload: TokenRequest, session: AsyncSession = Depends(get_session)
) -> None:
    _, customer = await consume_identity_token(session, payload.token, EMAIL_VERIFICATION_PURPOSE)
    customer.email_verified = True
    await session.commit()


@router.post(
    "/auth/password-reset-requests",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="请求邮箱重置密码",
)
async def request_password_reset(
    payload: EmailRequest, session: AsyncSession = Depends(get_session)
) -> MessageResponse:
    customer = await session.scalar(select(Customer).where(Customer.email == payload.email))
    await record_email_delivery_request(session, payload.email, PASSWORD_RESET_PURPOSE, customer)
    settings = get_customer_auth_settings()
    if not settings.email_delivery_enabled:
        await session.commit()
        return MessageResponse(message="如该邮箱可用，找回请求已记录；邮件服务接入后将发送重置链接。")
    if customer is None or not customer.is_active or not customer.email_verified:
        await session.commit()
        return MessageResponse(message="如果该邮箱可用，我们已发送重置密码的下一步说明。")
    token = await issue_identity_token(
        session,
        customer,
        PASSWORD_RESET_PURPOSE,
        settings.password_reset_token_ttl_seconds,
    )
    await session.commit()
    await send_or_fail(deliver_password_reset_email(customer, token))
    return MessageResponse(message="如果该邮箱可用，我们已发送重置密码的下一步说明。")


@router.post(
    "/auth/password-resets", status_code=status.HTTP_204_NO_CONTENT, summary="通过邮箱重置密码"
)
async def reset_password(
    payload: PasswordResetRequest, session: AsyncSession = Depends(get_session)
) -> None:
    _, customer = await consume_identity_token(session, payload.token, PASSWORD_RESET_PURPOSE)
    customer.password_hash = hash_password(payload.password)
    customer.password_changed_at = datetime.now(UTC)
    customer.token_version += 1
    await revoke_customer_sessions(session, customer.id)
    await session.commit()


@router.post("/auth/login", response_model=LoginResponse, summary="前台用户登录")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> LoginResponse:
    key = attempt_key(request, payload.username)
    if login_attempt_limiter.is_blocked(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="尝试次数过多，请稍后再试",
        )
    customer = await session.scalar(select(Customer).where(Customer.username == payload.username))
    if (
        customer is None
        or not customer.is_active
        or (
            get_customer_auth_settings().email_delivery_enabled
            and customer.email is not None
            and not customer.email_verified
        )
        or not verify_password(payload.password, customer.password_hash)
    ):
        login_attempt_limiter.record_failure(key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码不正确")
    login_attempt_limiter.clear(key)
    access_token, refresh_token, csrf_token = await issue_session(session, customer)
    await session.commit()
    set_refresh_cookies(response, refresh_token, csrf_token)
    return LoginResponse(
        access_token=access_token,
        expires_in=get_customer_auth_settings().access_token_ttl_seconds,
        user=customer_response(customer),
    )


@router.post("/auth/refresh", response_model=LoginResponse, summary="续期前台用户登录")
async def refresh(
    request: Request,
    response: Response,
    customer_refresh_token: str = Cookie(default=""),
    customer_csrf_token: str = Cookie(default=""),
    session: AsyncSession = Depends(get_session),
) -> LoginResponse:
    csrf_header = request.headers.get("x-csrf-token", "")
    record = await session.scalar(
        select(CustomerRefreshSession).where(
            CustomerRefreshSession.token_hash == hash_opaque_token(customer_refresh_token)
        )
    )
    now = datetime.now(UTC)
    if (
        record is None
        or not customer_refresh_token
        or not csrf_header
        or csrf_header != customer_csrf_token
        or record.revoked_at is not None
        or as_utc(record.expires_at) < now
        or record.csrf_hash != hash_opaque_token(csrf_header)
    ):
        clear_refresh_cookies(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh session is invalid"
        )
    customer = await session.get(Customer, record.customer_id)
    if customer is None or not customer.is_active:
        record.revoked_at = now
        await session.commit()
        clear_refresh_cookies(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh session is invalid"
        )
    record.revoked_at = now
    access_token, refresh_token, csrf_token = await issue_session(session, customer)
    await session.commit()
    set_refresh_cookies(response, refresh_token, csrf_token)
    return LoginResponse(
        access_token=access_token,
        expires_in=get_customer_auth_settings().access_token_ttl_seconds,
        user=customer_response(customer),
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, summary="退出前台用户登录")
async def logout(
    request: Request,
    response: Response,
    customer_refresh_token: str = Cookie(default=""),
    customer_csrf_token: str = Cookie(default=""),
    session: AsyncSession = Depends(get_session),
) -> None:
    csrf_header = request.headers.get("x-csrf-token", "")
    if customer_refresh_token and csrf_header and csrf_header == customer_csrf_token:
        record = await session.scalar(
            select(CustomerRefreshSession).where(
                CustomerRefreshSession.token_hash == hash_opaque_token(customer_refresh_token)
            )
        )
        if record is not None and record.csrf_hash == hash_opaque_token(csrf_header):
            record.revoked_at = datetime.now(UTC)
            await session.commit()
    clear_refresh_cookies(response)


@router.put("/auth/password", status_code=status.HTTP_204_NO_CONTENT, summary="登录态下修改密码")
async def change_password(
    payload: PasswordChangeRequest,
    customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> None:
    if not verify_password(payload.current_password, customer.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前密码不正确")
    customer.password_hash = hash_password(payload.new_password)
    customer.password_changed_at = datetime.now(UTC)
    customer.token_version += 1
    await revoke_customer_sessions(session, customer.id)
    await session.commit()


@router.get("/auth/me", response_model=CustomerResponse, summary="获取当前前台用户")
async def me(customer: Customer = Depends(get_current_customer)) -> CustomerResponse:
    return customer_response(customer)
