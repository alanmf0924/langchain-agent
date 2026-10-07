from __future__ import annotations

import asyncio
import os

from sqlalchemy import select

from app.customer.database import SessionLocal
from app.customer.models import Customer
from app.customer.security import hash_password


async def bootstrap() -> None:
    """仅在迁移完成后按部署 Secret 创建第一位前台用户。"""
    username = os.getenv("CUSTOMER_BOOTSTRAP_USERNAME", "")
    password = os.getenv("CUSTOMER_BOOTSTRAP_PASSWORD", "")
    display_name = os.getenv("CUSTOMER_BOOTSTRAP_DISPLAY_NAME", "澄肌用户")
    if not username or not password:
        raise RuntimeError(
            "CUSTOMER_BOOTSTRAP_USERNAME and CUSTOMER_BOOTSTRAP_PASSWORD are required"
        )
    async with SessionLocal() as session:
        if await session.scalar(select(Customer).where(Customer.username == username)):
            return
        session.add(
            Customer(
                username=username,
                password_hash=hash_password(password),
                display_name=display_name,
            )
        )
        await session.commit()


if __name__ == "__main__":
    asyncio.run(bootstrap())
