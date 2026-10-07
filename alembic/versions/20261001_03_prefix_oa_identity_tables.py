"""prefix OA identity tables to isolate them from storefront users

Revision ID: 20261001_03
Revises: 20261001_02
Create Date: 2026-10-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261001_03"
down_revision: str | None = "20261001_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 采用重命名而不是重建：已 bootstrap 的管理员、角色与会话审计都必须原样保留。
TABLE_RENAMES = (
    ("departments", "oa_departments"),
    ("roles", "oa_roles"),
    ("permissions", "oa_permissions"),
    ("users", "oa_users"),
    ("user_roles", "oa_user_roles"),
    ("role_permissions", "oa_role_permissions"),
    ("refresh_sessions", "oa_refresh_sessions"),
)
INDEX_RENAMES = (
    ("ix_users_username", "ix_oa_users_username"),
    ("ix_roles_code", "ix_oa_roles_code"),
    ("ix_permissions_code", "ix_oa_permissions_code"),
    ("ix_refresh_sessions_user_id", "ix_oa_refresh_sessions_user_id"),
    ("ix_refresh_sessions_token_hash", "ix_oa_refresh_sessions_token_hash"),
    ("ix_refresh_sessions_expires_at", "ix_oa_refresh_sessions_expires_at"),
)


def upgrade() -> None:
    for old_name, new_name in TABLE_RENAMES:
        op.rename_table(old_name, new_name)
    # PostgreSQL 支持索引改名；SQLite 的 ALTER INDEX 不支持该语法，且索引旧名不影响表迁移。
    if op.get_bind().dialect.name != "sqlite":
        for old_name, new_name in INDEX_RENAMES:
            op.execute(f"ALTER INDEX {old_name} RENAME TO {new_name}")


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        for old_name, new_name in reversed(INDEX_RENAMES):
            op.execute(f"ALTER INDEX {new_name} RENAME TO {old_name}")
    for old_name, new_name in reversed(TABLE_RENAMES):
        op.rename_table(new_name, old_name)
