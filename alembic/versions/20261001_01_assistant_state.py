"""persist assistant state in the shared PostgreSQL database

Revision ID: 20261001_01
Revises:
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261001_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_traces",
        sa.Column("run_id", sa.String(80), primary_key=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "assistant_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(80), nullable=False),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("input_summary", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
    )
    op.create_index("ix_assistant_audit_logs_run_id", "assistant_audit_logs", ["run_id"])
    op.create_table(
        "model_usage_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(80), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_model_usage_logs_run_id", "model_usage_logs", ["run_id"])
    op.create_table(
        "confirmation_tokens",
        sa.Column("token", sa.String(160), primary_key=True),
        sa.Column("run_id", sa.String(80), nullable=False),
        sa.Column("bundle_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
    )
    op.create_index("ix_confirmation_tokens_expires_at", "confirmation_tokens", ["expires_at"])
    op.create_index("ix_confirmation_tokens_state", "confirmation_tokens", ["state"])
    op.create_table(
        "cart_drafts",
        sa.Column("cart_draft_id", sa.String(100), primary_key=True),
        sa.Column("confirmation_token", sa.String(160), nullable=False, unique=True),
        sa.Column("bundle_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("price_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("cart_drafts")
    op.drop_table("confirmation_tokens")
    op.drop_table("model_usage_logs")
    op.drop_table("assistant_audit_logs")
    op.drop_table("agent_traces")
