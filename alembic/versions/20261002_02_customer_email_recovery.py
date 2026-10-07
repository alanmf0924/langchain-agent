"""add storefront customer email verification and recovery

Revision ID: 20261002_02
Revises: 20261002_01
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261002_02"
down_revision: str | None = "20261002_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 旧 bootstrap 账号没有邮箱，迁移时不能直接设为 NOT NULL。
    op.add_column("customers", sa.Column("email", sa.String(254), nullable=True))
    op.add_column(
        "customers",
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "customers", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_customers_email", "customers", ["email"], unique=True)
    op.create_table(
        "customer_identity_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_customer_identity_tokens_customer_id",
        "customer_identity_tokens",
        ["customer_id"],
    )
    op.create_index("ix_customer_identity_tokens_purpose", "customer_identity_tokens", ["purpose"])
    op.create_index(
        "ix_customer_identity_tokens_token_hash", "customer_identity_tokens", ["token_hash"]
    )
    op.create_index(
        "ix_customer_identity_tokens_expires_at", "customer_identity_tokens", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("customer_identity_tokens")
    op.drop_index("ix_customers_email", table_name="customers")
    op.drop_column("customers", "password_changed_at")
    op.drop_column("customers", "email_verified")
    op.drop_column("customers", "email")
