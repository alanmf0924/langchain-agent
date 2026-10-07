from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.oa.config import get_oa_settings
from app.oa.models import Department, OaAuditLog, RefreshSession, Role, User
from app.oa.schemas import CurrentUserResponse
from app.oa.security import create_access_token, create_opaque_token, hash_opaque_token


async def load_user(session: AsyncSession, user_id: str) -> User | None:
    statement = (
        select(User)
        .options(
            selectinload(User.roles).selectinload(Role.permissions),
            selectinload(User.permission_grants),
        )
        .where(User.id == user_id)
    )
    return await session.scalar(statement)


def permission_codes(user: User) -> list[str]:
    # 权限变更立即在每次鉴权时生效；失效角色不能继续授权已有 access token。
    return sorted(
        {
            permission.code
            for role in user.roles
            if role.is_active
            for permission in role.permissions
        }
        | {permission.code for permission in user.permission_grants}
    )


def data_scope(user: User, permission_code: str) -> str:
    """返回某一权限的最宽角色数据范围；直接例外权限不扩大数据范围。"""
    scope_rank = {"self": 0, "department": 1, "all": 2}
    active_scopes = [
        role.data_scope
        for role in user.roles
        if role.is_active and permission_code in {permission.code for permission in role.permissions}
    ]
    if not active_scopes:
        return "self"
    return max(active_scopes, key=lambda item: scope_rank.get(item, 0))


async def is_active_account(session: AsyncSession, user: User) -> bool:
    """部门停用与账号停用同样阻断登录、续期和每一次受保护请求。"""
    if not user.is_active:
        return False
    department_is_active = await session.scalar(
        select(Department.is_active).where(Department.id == user.department_id)
    )
    return department_is_active is True


def current_user_response(user: User) -> CurrentUserResponse:
    return CurrentUserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role_codes=sorted(role.code for role in user.roles if role.is_active),
        permission_codes=permission_codes(user),
    )


async def write_audit_log(
    session: AsyncSession, *, actor_id: str | None, action: str, outcome: str, detail: str = ""
) -> None:
    session.add(OaAuditLog(actor_id=actor_id, action=action, outcome=outcome, detail=detail))


async def revoke_user_sessions(session: AsyncSession, user_id: str) -> None:
    """账号停用、改密或变更角色时撤销长期会话，避免旧 refresh token 继续换新令牌。"""
    sessions = (
        await session.scalars(
            select(RefreshSession).where(
                RefreshSession.user_id == user_id, RefreshSession.revoked_at.is_(None)
            )
        )
    ).all()
    now = datetime.now(UTC)
    for item in sessions:
        item.revoked_at = now


async def issue_session(session: AsyncSession, user: User) -> tuple[str, str, str]:
    settings = get_oa_settings()
    refresh_token, csrf_token = create_opaque_token(), create_opaque_token()
    session.add(
        RefreshSession(
            user_id=user.id,
            token_hash=hash_opaque_token(refresh_token),
            csrf_hash=hash_opaque_token(csrf_token),
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
    )
    await session.flush()
    return create_access_token(user.id, user.token_version), refresh_token, csrf_token
