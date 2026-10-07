from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.oa.database import get_session
from app.oa.models import User
from app.oa.security import decode_access_token
from app.oa.services import load_user, permission_codes

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录后再继续操作"
        )
    try:
        payload = decode_access_token(credentials.credentials)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录凭证无效，请重新登录"
        ) from error
    user = await load_user(session, payload["sub"])
    if user is None or not user.is_active or user.token_version != payload.get("tv"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录会话已失效，请重新登录"
        )
    return user


def require_permission(code: str) -> Callable[[User], User]:
    async def checker(user: User = Depends(get_current_user)) -> User:
        if code not in permission_codes(user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前账号没有此操作权限")
        return user

    return checker
