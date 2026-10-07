"""restrict permission resources to page and action kinds

Revision ID: 20261004_09
Revises: 20261004_08
Create Date: 2026-10-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261004_09"
down_revision: str | None = "20261004_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 旧的 menu 资源没有服务端路由绑定语义，统一转为页面资源后再由明确页面权限下发。
    op.execute("UPDATE oa_permissions SET kind = 'page' WHERE kind = 'menu'")


def downgrade() -> None:
    # 无法从已发布的页面资源可靠推断其是否曾为 menu，故保留 page 类型。
    pass
