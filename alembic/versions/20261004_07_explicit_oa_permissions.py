"""replace wildcard OA grants with explicit page and action permissions

Revision ID: 20261004_07
Revises: 20261004_06
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261004_07"
down_revision: str | None = "20261004_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


PERMISSIONS = (
    ("catalog:product:read", "查看商品", "page"),
    ("catalog:product:write", "维护商品", "action"),
    ("system:user:read", "查看登录账号", "page"),
    ("system:user:write", "维护登录账号", "action"),
    ("system:department:read", "查看部门", "page"),
    ("system:department:write", "维护部门", "action"),
    ("system:personnel:read", "查看人员档案", "page"),
    ("system:personnel:write", "维护人员档案", "action"),
    ("system:authorization:read", "查看组织授权", "page"),
    ("system:authorization:write", "维护组织授权", "action"),
    ("system:permission:read", "查看权限资源", "page"),
    ("system:permission:write", "维护权限资源", "action"),
    ("system:role:read", "查看角色", "page"),
    ("system:role:write", "维护角色", "action"),
    ("assistant:knowledge:read", "查看知识库", "page"),
    ("assistant:knowledge:write", "维护知识库", "action"),
    ("assistant:operations:read", "查看助手运行审计", "page"),
    ("assistant:evaluation:read", "查看评测报告", "page"),
    ("assistant:evaluation:write", "执行评测", "action"),
)


def upgrade() -> None:
    op.add_column("catalog_product_images", sa.Column("created_by", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_catalog_product_images_created_by",
        "catalog_product_images",
        "oa_users",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_catalog_product_images_created_by", "catalog_product_images", ["created_by"])

    for code, name, kind in PERMISSIONS:
        op.execute(
            sa.text(
                """
                INSERT INTO oa_permissions (id, code, name, kind)
                SELECT md5(random()::text || clock_timestamp()::text), :code, :name, :kind
                WHERE NOT EXISTS (SELECT 1 FROM oa_permissions WHERE code = :code)
                """
            ).bindparams(code=code, name=name, kind=kind)
        )

    # 超级管理员保留现有能力，但以所有明确资源的集合表达，不再通过通配符越权。
    op.execute(
        """
        INSERT INTO oa_role_permissions (role_id, permission_id)
        SELECT roles.id, permissions.id
        FROM oa_roles AS roles
        CROSS JOIN oa_permissions AS permissions
        WHERE roles.code = 'super_admin'
          AND permissions.code <> '*:*:*'
        ON CONFLICT DO NOTHING
        """
    )
    # 开发人员（order）仅保留商品页面及其维护按钮；数据范围沿用既有 all 配置。
    op.execute(
        """
        INSERT INTO oa_role_permissions (role_id, permission_id)
        SELECT roles.id, permissions.id
        FROM oa_roles AS roles
        JOIN oa_permissions AS permissions ON permissions.code IN ('catalog:product:read', 'catalog:product:write')
        WHERE roles.code = 'order'
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        DELETE FROM oa_role_permissions
        WHERE permission_id IN (SELECT id FROM oa_permissions WHERE code = '*:*:*')
        """
    )
    op.execute("DELETE FROM oa_permissions WHERE code = '*:*:*'")
    # 旧令牌不能继续携带已经废弃的通配授权。
    op.execute("UPDATE oa_users SET token_version = token_version + 1")
    op.execute("UPDATE oa_refresh_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE revoked_at IS NULL")


def downgrade() -> None:
    op.drop_index("ix_catalog_product_images_created_by", table_name="catalog_product_images")
    op.drop_constraint(
        "fk_catalog_product_images_created_by", "catalog_product_images", type_="foreignkey"
    )
    op.drop_column("catalog_product_images", "created_by")
