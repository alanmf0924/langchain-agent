"""persist customer-owned consultation conversations

Revision ID: 20261005_11
Revises: 20261005_10
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261005_11"
down_revision: str | None = "20261005_10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "customer_conversations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("handoff_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("handoff_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'handoff_pending', 'archived')",
            name="ck_customer_conversations_status",
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_conversations_customer_id", "customer_conversations", ["customer_id"])
    op.create_index("ix_customer_conversations_status", "customer_conversations", ["status"])
    op.create_index("ix_customer_conversations_updated_at", "customer_conversations", ["updated_at"])

    op.create_table(
        "customer_conversation_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False, server_default="complete"),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_customer_conversation_messages_role"),
        sa.CheckConstraint(
            "phase IN ('pending', 'complete', 'failed')",
            name="ck_customer_conversation_messages_phase",
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["customer_conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_customer_conversation_messages_conversation_id",
        "customer_conversation_messages",
        ["conversation_id"],
    )
    op.create_index(
        "ix_customer_conversation_messages_created_at",
        "customer_conversation_messages",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_customer_conversation_messages_created_at", table_name="customer_conversation_messages")
    op.drop_index(
        "ix_customer_conversation_messages_conversation_id",
        table_name="customer_conversation_messages",
    )
    op.drop_table("customer_conversation_messages")
    op.drop_index("ix_customer_conversations_updated_at", table_name="customer_conversations")
    op.drop_index("ix_customer_conversations_status", table_name="customer_conversations")
    op.drop_index("ix_customer_conversations_customer_id", table_name="customer_conversations")
    op.drop_table("customer_conversations")
