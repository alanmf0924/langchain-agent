"""add non-login personnel records for organization management

Revision ID: 20261004_05
Revises: 20261004_04
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261004_05"
down_revision: str | None = "20261004_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "oa_personnel",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False, server_default=""),
        sa.Column("email", sa.String(254), nullable=False, server_default=""),
        sa.Column("gender", sa.String(16), nullable=False, server_default="unspecified"),
        sa.Column("department_id", sa.String(36), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.CheckConstraint("gender IN ('female', 'male', 'unspecified')", name="ck_oa_personnel_gender"),
        sa.ForeignKeyConstraint(["department_id"], ["oa_departments.id"]),
    )
    op.create_index("ix_oa_personnel_name", "oa_personnel", ["name"])
    op.create_index("ix_oa_personnel_department_id", "oa_personnel", ["department_id"])
    op.create_index("ix_oa_personnel_is_active", "oa_personnel", ["is_active"])


def downgrade() -> None:
    op.drop_table("oa_personnel")
