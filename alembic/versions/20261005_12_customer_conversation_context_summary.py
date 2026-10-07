"""add bounded customer consultation context summary

Revision ID: 20261005_12
Revises: 20261005_11
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261005_12"
down_revision: str | None = "20261005_11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "customer_conversations",
        sa.Column("context_summary_json", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("customer_conversations", "context_summary_json")
