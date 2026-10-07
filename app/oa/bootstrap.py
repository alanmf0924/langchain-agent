from __future__ import annotations

import asyncio
import os

from sqlalchemy import select

from app.oa.database import SessionLocal
from app.oa.models import Department, Permission, Role, User, new_id
from app.oa.permission_catalog import PERMISSION_DEFINITIONS
from app.oa.security import hash_password


async def bootstrap() -> None:
    """只在已迁移的库内一次性创建管理员；账号口令必须来自部署 Secret。"""
    username, password = (
        os.getenv("OA_BOOTSTRAP_USERNAME", ""),
        os.getenv("OA_BOOTSTRAP_PASSWORD", ""),
    )
    if not username or not password:
        raise RuntimeError("OA_BOOTSTRAP_USERNAME and OA_BOOTSTRAP_PASSWORD are required")
    async with SessionLocal() as session:
        if await session.scalar(select(User).where(User.username == username)):
            return
        department = Department(id=new_id(), name="平台管理部", parent_id="")
        permissions = [
            Permission(id=new_id(), code=code, name=name, kind=kind)
            for code, name, kind in PERMISSION_DEFINITIONS
        ]
        role = Role(
            code="super_admin", name="超级管理员", data_scope="all", permissions=permissions
        )
        user = User(
            username=username,
            display_name="平台管理员",
            password_hash=hash_password(password),
            department_id=department.id,
            roles=[role],
        )
        session.add_all([department, *permissions, role, user])
        await session.commit()


if __name__ == "__main__":
    asyncio.run(bootstrap())
