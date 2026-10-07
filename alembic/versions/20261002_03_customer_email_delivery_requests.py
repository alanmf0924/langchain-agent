"""record storefront email requests before delivery integration

Revision ID: 20261002_03
Revises: 20261002_02
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261002_03"
down_revision: str | None = "20261002_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "customer_email_delivery_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(36), nullable=True),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("recipient_hash", sa.String(64), nullable=False),
        sa.Column(
            "delivery_status", sa.String(32), nullable=False, server_default="pending_delivery"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_customer_email_delivery_requests_customer_id",
        "customer_email_delivery_requests",
        ["customer_id"],
    )
    op.create_index(
        "ix_customer_email_delivery_requests_purpose",
        "customer_email_delivery_requests",
        ["purpose"],
    )
    op.create_index(
        "ix_customer_email_delivery_requests_recipient_hash",
        "customer_email_delivery_requests",
        ["recipient_hash"],
    )


def downgrade() -> None:
    op.drop_table("customer_email_delivery_requests")
