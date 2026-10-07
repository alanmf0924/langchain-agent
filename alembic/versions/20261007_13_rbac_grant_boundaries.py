"""add explicit global delegation permissions for RBAC boundary enforcement

Revision ID: 20261007_13
Revises: 20261005_12
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261007_13"
down_revision: str | None = "20261005_12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


PERMISSIONS = (
    ("system:authorization:grant_any", "授予全部权限", "action"),
    ("system:role:manage_any", "管理全部角色", "action"),
    ("system:user:manage_any", "管理全部登录账号", "action"),
)


def upgrade() -> None:
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
    op.execute(
        """
        INSERT INTO oa_role_permissions (role_id, permission_id)
        SELECT roles.id, permissions.id
        FROM oa_roles AS roles
        JOIN oa_permissions AS permissions
          ON permissions.code IN (
            'system:authorization:grant_any',
            'system:role:manage_any',
            'system:user:manage_any'
          )
        WHERE roles.code IN ('super_admin', 'admin', 'system_admin')
        ON CONFLICT DO NOTHING
        """
    )
    op.execute("UPDATE oa_users SET token_version = token_version + 1")
    op.execute("UPDATE oa_refresh_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE revoked_at IS NULL")


def downgrade() -> None:
    codes = ", ".join(f"'{code}'" for code, _, _ in PERMISSIONS)
    op.execute(
        f"DELETE FROM oa_role_permissions WHERE permission_id IN (SELECT id FROM oa_permissions WHERE code IN ({codes}))"
    )
    op.execute(
        f"DELETE FROM oa_user_permission_grants WHERE permission_id IN (SELECT id FROM oa_permissions WHERE code IN ({codes}))"
    )
    op.execute(f"DELETE FROM oa_permissions WHERE code IN ({codes})")
