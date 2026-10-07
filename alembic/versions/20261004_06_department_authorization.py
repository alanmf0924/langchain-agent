"""add department role constraints and personnel exception grants

Revision ID: 20261004_06
Revises: 20261004_05
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261004_06"
down_revision: str | None = "20261004_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "oa_department_roles",
        sa.Column("department_id", sa.String(36), nullable=False),
        sa.Column("role_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["department_id"], ["oa_departments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["oa_roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("department_id", "role_id"),
    )
    op.create_table(
        "oa_user_permission_grants",
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("permission_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["oa_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["permission_id"], ["oa_permissions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "permission_id"),
    )
    op.add_column("oa_personnel", sa.Column("user_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_oa_personnel_user_id", "oa_personnel", "oa_users", ["user_id"], ["id"], ondelete="SET NULL"
    )
    op.create_unique_constraint("uq_oa_personnel_user_id", "oa_personnel", ["user_id"])
    # 将现有账号已有的角色映射到其部门，避免发布后现有管理员和运营账号失去可分配资格。
    op.execute(
        """
        INSERT INTO oa_department_roles (department_id, role_id)
        SELECT DISTINCT users.department_id, user_roles.role_id
        FROM oa_users AS users
        JOIN oa_user_roles AS user_roles ON user_roles.user_id = users.id
        WHERE users.department_id <> ''
        ON CONFLICT DO NOTHING
        """
    )
    # 新能力可由超级管理员直接使用，也可在既有权限资源页面中授予其他管理角色。
    op.execute(
        """
        INSERT INTO oa_permissions (id, code, name, kind)
        SELECT md5(random()::text || clock_timestamp()::text), 'system:authorization:read', '查看组织授权', 'action'
        WHERE NOT EXISTS (SELECT 1 FROM oa_permissions WHERE code = 'system:authorization:read')
        """
    )
    op.execute(
        """
        INSERT INTO oa_permissions (id, code, name, kind)
        SELECT md5(random()::text || clock_timestamp()::text), 'system:authorization:write', '管理组织授权', 'action'
        WHERE NOT EXISTS (SELECT 1 FROM oa_permissions WHERE code = 'system:authorization:write')
        """
    )


def downgrade() -> None:
    op.drop_constraint("uq_oa_personnel_user_id", "oa_personnel", type_="unique")
    op.drop_constraint("fk_oa_personnel_user_id", "oa_personnel", type_="foreignkey")
    op.drop_column("oa_personnel", "user_id")
    op.drop_table("oa_user_permission_grants")
    op.drop_table("oa_department_roles")
