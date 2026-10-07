"""测试默认不触发外部向量服务，单元测试用假客户端覆盖即可。"""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("RAG_EMBEDDER", "lexical")
os.environ.setdefault("APP_ENV", "test")

from app.oa.dependencies import get_current_user
from app.oa.permission_catalog import ALL_PERMISSION_CODES


@pytest.fixture
def oa_operations_access():
    """非权限主题的 API 测试用明确的 OA 超级权限绕过认证运输层。"""
    from app.main import app

    user = SimpleNamespace(
        roles=[
            SimpleNamespace(
                is_active=True,
                data_scope="all",
                permissions=[SimpleNamespace(code=code) for code in ALL_PERMISSION_CODES],
            )
        ],
        permission_grants=[],
    )
    app.dependency_overrides[get_current_user] = lambda: user
    yield
    app.dependency_overrides.pop(get_current_user, None)
