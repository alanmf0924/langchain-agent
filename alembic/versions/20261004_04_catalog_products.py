"""add managed catalog products and product images

Revision ID: 20261004_04
Revises: 20261002_03
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261004_04"
down_revision: str | None = "20261002_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_products",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sku_id", sa.String(64), nullable=False, unique=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("spec", sa.String(120), nullable=False, server_default=""),
        sa.Column("price_fen", sa.Integer(), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("manual", sa.Text(), nullable=False, server_default=""),
        sa.Column("ingredients", sa.JSON(), nullable=False),
        sa.Column("ingredient_disclosure_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("usage", sa.Text(), nullable=False, server_default=""),
        sa.Column("cautions", sa.Text(), nullable=False, server_default=""),
        sa.Column("category_code", sa.String(64), nullable=False, server_default=""),
        sa.Column("scenario_tags", sa.JSON(), nullable=False),
        sa.Column("on_sale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("assistant_approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.CheckConstraint("price_fen >= 0", name="ck_catalog_products_price_nonnegative"),
        sa.CheckConstraint("stock >= 0", name="ck_catalog_products_stock_nonnegative"),
        sa.ForeignKeyConstraint(["created_by"], ["oa_users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_catalog_products_sku_id", "catalog_products", ["sku_id"])
    op.create_index("ix_catalog_products_on_sale", "catalog_products", ["on_sale"])
    op.create_index("ix_catalog_products_assistant_approved", "catalog_products", ["assistant_approved"])
    op.create_table(
        "catalog_product_images",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("product_id", sa.String(36), nullable=True),
        sa.Column("storage_key", sa.String(255), nullable=False, unique=True),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_catalog_images_nonempty"),
        sa.ForeignKeyConstraint(["product_id"], ["catalog_products.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_catalog_product_images_product_id", "catalog_product_images", ["product_id"])


def downgrade() -> None:
    op.drop_table("catalog_product_images")
    op.drop_table("catalog_products")
