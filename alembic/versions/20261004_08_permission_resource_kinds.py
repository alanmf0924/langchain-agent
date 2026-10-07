"""align existing permission resource kinds with page and action semantics

Revision ID: 20261004_08
Revises: 20261004_07
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261004_08"
down_revision: str | None = "20261004_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE oa_permissions
            SET name = :name, kind = :kind
            WHERE code = :code
            """
        ).bindparams(
            code="system:authorization:read", name="查看组织授权", kind="page"
        )
    )


def downgrade() -> None:
    op.execute(
        "UPDATE oa_permissions SET kind = 'action' WHERE code = 'system:authorization:read'"
    )
