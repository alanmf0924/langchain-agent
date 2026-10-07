"""add customer addresses and auditable commerce orders

Revision ID: 20261005_10
Revises: 20261004_09
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261005_10"
down_revision: str | None = "20261004_09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


COMMERCE_PERMISSIONS = (
    ("customer:profile:read", "查看前台客户", "page"),
    ("order:record:read", "查看商城订单", "page"),
    ("order:record:write", "处理商城订单", "action"),
)


def upgrade() -> None:
    op.create_table(
        "customer_addresses",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("recipient_name", sa.String(length=80), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("province", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("city", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("district", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("detail", sa.String(length=255), nullable=False),
        sa.Column("postal_code", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
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
            "length(trim(recipient_name)) > 0", name="ck_customer_addresses_recipient_name"
        ),
        sa.CheckConstraint("length(trim(phone)) > 0", name="ck_customer_addresses_phone"),
        sa.CheckConstraint("length(trim(detail)) > 0", name="ck_customer_addresses_detail"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_addresses_customer_id", "customer_addresses", ["customer_id"])

    op.create_table(
        "customer_orders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("order_no", sa.String(length=48), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending_payment"),
        sa.Column("total_fen", sa.Integer(), nullable=False),
        sa.Column("recipient_name", sa.String(length=80), nullable=False),
        sa.Column("recipient_phone", sa.String(length=32), nullable=False),
        sa.Column("recipient_province", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("recipient_city", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("recipient_district", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("recipient_detail", sa.String(length=255), nullable=False),
        sa.Column("recipient_postal_code", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("customer_remark", sa.Text(), nullable=False, server_default=""),
        sa.Column("fulfillment_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("tracking_no", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("total_fen >= 0", name="ck_customer_orders_total_fen"),
        sa.CheckConstraint(
            "status IN ('pending_payment', 'paid', 'fulfilling', 'shipped', 'completed', 'cancelled')",
            name="ck_customer_orders_status",
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_no"),
    )
    op.create_index("ix_customer_orders_customer_id", "customer_orders", ["customer_id"])
    op.create_index("ix_customer_orders_status", "customer_orders", ["status"])
    op.create_index("ix_customer_orders_order_no", "customer_orders", ["order_no"])

    op.create_table(
        "customer_order_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("sku_id", sa.String(length=64), nullable=False),
        sa.Column("product_title", sa.String(length=160), nullable=False),
        sa.Column("product_spec", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("unit_price_fen", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("subtotal_fen", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("unit_price_fen >= 0", name="ck_customer_order_items_unit_price_fen"),
        sa.CheckConstraint("quantity > 0", name="ck_customer_order_items_quantity"),
        sa.CheckConstraint("subtotal_fen >= 0", name="ck_customer_order_items_subtotal_fen"),
        sa.ForeignKeyConstraint(["order_id"], ["customer_orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_order_items_order_id", "customer_order_items", ["order_id"])
    op.create_index("ix_customer_order_items_product_id", "customer_order_items", ["product_id"])

    for code, name, kind in COMMERCE_PERMISSIONS:
        op.execute(
            sa.text(
                """
                INSERT INTO oa_permissions (id, code, name, kind)
                SELECT md5(random()::text || clock_timestamp()::text), :code, :name, :kind
                WHERE NOT EXISTS (SELECT 1 FROM oa_permissions WHERE code = :code)
                """
            ).bindparams(code=code, name=name, kind=kind)
        )

    # 既有超级管理员自动获得新增资源；其他角色需在“角色管理”中显式授予。
    op.execute(
        """
        INSERT INTO oa_role_permissions (role_id, permission_id)
        SELECT roles.id, permissions.id
        FROM oa_roles AS roles
        JOIN oa_permissions AS permissions
          ON permissions.code IN ('customer:profile:read', 'order:record:read', 'order:record:write')
        WHERE roles.code = 'super_admin'
        ON CONFLICT DO NOTHING
        """
    )
    # 路由权限在登录时计算，令牌版本升级同时让旧 refresh 会话失效并重新拉取菜单。
    op.execute("UPDATE oa_users SET token_version = token_version + 1")
    op.execute(
        "UPDATE oa_refresh_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE revoked_at IS NULL"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM oa_role_permissions WHERE permission_id IN "
        "(SELECT id FROM oa_permissions WHERE code IN ('customer:profile:read', 'order:record:read', 'order:record:write'))"
    )
    op.execute(
        "DELETE FROM oa_permissions WHERE code IN ('customer:profile:read', 'order:record:read', 'order:record:write')"
    )
    op.drop_index("ix_customer_order_items_product_id", table_name="customer_order_items")
    op.drop_index("ix_customer_order_items_order_id", table_name="customer_order_items")
    op.drop_table("customer_order_items")
    op.drop_index("ix_customer_orders_order_no", table_name="customer_orders")
    op.drop_index("ix_customer_orders_status", table_name="customer_orders")
    op.drop_index("ix_customer_orders_customer_id", table_name="customer_orders")
    op.drop_table("customer_orders")
    op.drop_index("ix_customer_addresses_customer_id", table_name="customer_addresses")
    op.drop_table("customer_addresses")
