"""数据库连接配置：生产运行只允许 PostgreSQL，测试可显式注入 SQLite。"""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool


def database_url() -> str:
    """读取唯一的服务数据库地址，避免按业务模块拆分出多个事实源。"""
    url = os.getenv("DATABASE_URL", "")
    environment = os.getenv("APP_ENV", "development")
    if not url:
        if environment == "test":
            return "sqlite+pysqlite:///:memory:"
        raise RuntimeError("DATABASE_URL is required; production must use PostgreSQL")
    if environment == "production" and not url.startswith("postgresql+"):
        raise RuntimeError("DATABASE_URL must use a PostgreSQL SQLAlchemy driver in production")
    return url


def create_database_engine(url: str | None = None) -> Engine:
    """创建具备连接存活检测的同步引擎，供现有同步 Agent 工作流安全复用。"""
    resolved_url = url or database_url()
    if resolved_url.startswith("sqlite+aiosqlite://"):
        # OA 模块的异步 SQLite 仅用于测试；助手同步 Repository 复用同一测试库时改用同步驱动。
        resolved_url = resolved_url.replace("sqlite+aiosqlite://", "sqlite+pysqlite://", 1)
    if resolved_url == "sqlite+pysqlite:///:memory:":
        # 单测要跨 Repository 的多个连接读写同一个内存库；生产不会走该分支。
        return create_engine(
            resolved_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_engine(resolved_url, pool_pre_ping=True)
