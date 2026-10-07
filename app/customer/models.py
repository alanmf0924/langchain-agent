from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_customer_id() -> str:
    return str(uuid4())


class CustomerBase(DeclarativeBase):
    pass


class Customer(CustomerBase):
    """商城前台用户，与 OA 员工身份表严格隔离。"""

    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_customer_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 旧的 bootstrap 账号没有邮箱；迁移期间允许为空，所有自助注册账号必须提供并验证邮箱。
    email: Mapped[str | None] = mapped_column(String(254), unique=True, index=True, nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CustomerRefreshSession(CustomerBase):
    __tablename__ = "customer_refresh_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_customer_id)
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CustomerIdentityToken(CustomerBase):
    """一次性邮箱验证/改密令牌；数据库永远不保存明文 token。"""

    __tablename__ = "customer_identity_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_customer_id)
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(32), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CustomerConversation(CustomerBase):
    """前台客户自有咨询会话；绝不与 OA 员工身份或其他客户共享。"""

    __tablename__ = "customer_conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_customer_id)
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    # handoff_pending 表示自动问答已收口，等待后续人工客服系统接入处理。
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    handoff_reason: Mapped[str] = mapped_column(Text, default="")
    handoff_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 仅供服务端组装下一轮选购问题的短摘要；不回传前台，也不保存用户原句。
    context_summary_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class CustomerConversationMessage(CustomerBase):
    """会话中的用户和助手可见消息；结构化结果保存为 JSON 以支持刷新恢复。"""

    __tablename__ = "customer_conversation_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_customer_id)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customer_conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    phase: Mapped[str] = mapped_column(String(16), default="complete")
    # 不存模型思维链；仅保存已经通过 SSE 返回给用户的结构化公开结果。
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Python 端写入微秒级时间，保证同一秒内连续的用户/助手消息按真实顺序恢复。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now(), index=True
    )


class CustomerEmailDeliveryRequest(CustomerBase):
    """待接入邮件服务的请求记录；只保留邮箱摘要，不能反查地址。"""

    __tablename__ = "customer_email_delivery_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_customer_id)
    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    purpose: Mapped[str] = mapped_column(String(32), index=True)
    recipient_hash: Mapped[str] = mapped_column(String(64), index=True)
    delivery_status: Mapped[str] = mapped_column(String(32), default="pending_delivery")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CustomerAddress(CustomerBase):
    """客户自有的收货地址；订单会在创建时复制一份快照。"""

    __tablename__ = "customer_addresses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_customer_id)
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    recipient_name: Mapped[str] = mapped_column(String(80))
    phone: Mapped[str] = mapped_column(String(32))
    province: Mapped[str] = mapped_column(String(64), default="")
    city: Mapped[str] = mapped_column(String(64), default="")
    district: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str] = mapped_column(String(255))
    postal_code: Mapped[str] = mapped_column(String(20), default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CustomerOrder(CustomerBase):
    """商城订单主表，金额、地址和商品内容均是下单时快照。"""

    __tablename__ = "customer_orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_customer_id)
    order_no: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending_payment", index=True)
    total_fen: Mapped[int] = mapped_column(Integer)
    recipient_name: Mapped[str] = mapped_column(String(80))
    recipient_phone: Mapped[str] = mapped_column(String(32))
    recipient_province: Mapped[str] = mapped_column(String(64), default="")
    recipient_city: Mapped[str] = mapped_column(String(64), default="")
    recipient_district: Mapped[str] = mapped_column(String(64), default="")
    recipient_detail: Mapped[str] = mapped_column(String(255))
    recipient_postal_code: Mapped[str] = mapped_column(String(20), default="")
    customer_remark: Mapped[str] = mapped_column(Text, default="")
    fulfillment_note: Mapped[str] = mapped_column(Text, default="")
    tracking_no: Mapped[str] = mapped_column(String(120), default="")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CustomerOrderItem(CustomerBase):
    """订单行只保留商品 ID 关联线索；真实履约数据以快照字段为准。"""

    __tablename__ = "customer_order_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_customer_id)
    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customer_orders.id", ondelete="CASCADE"), index=True
    )
    # 不建跨 metadata 的商品外键：商品可以下架或删除，订单快照仍必须可追溯。
    product_id: Mapped[str] = mapped_column(String(36), index=True)
    sku_id: Mapped[str] = mapped_column(String(64))
    product_title: Mapped[str] = mapped_column(String(160))
    product_spec: Mapped[str] = mapped_column(String(120), default="")
    unit_price_fen: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    subtotal_fen: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
