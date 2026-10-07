from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


user_roles = Table(
    "oa_user_roles",
    Base.metadata,
    Column("user_id", String(36), ForeignKey("oa_users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", String(36), ForeignKey("oa_roles.id", ondelete="CASCADE"), primary_key=True),
)
role_permissions = Table(
    "oa_role_permissions",
    Base.metadata,
    Column("role_id", String(36), ForeignKey("oa_roles.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "permission_id",
        String(36),
        ForeignKey("oa_permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)
department_roles = Table(
    "oa_department_roles",
    Base.metadata,
    Column(
        "department_id",
        String(36),
        ForeignKey("oa_departments.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "role_id", String(36), ForeignKey("oa_roles.id", ondelete="CASCADE"), primary_key=True
    ),
)
user_permission_grants = Table(
    "oa_user_permission_grants",
    Base.metadata,
    Column(
        "user_id", String(36), ForeignKey("oa_users.id", ondelete="CASCADE"), primary_key=True
    ),
    Column(
        "permission_id",
        String(36),
        ForeignKey("oa_permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Department(Base):
    # OA 员工组织与商城前台消费者数据隔离，禁止复用通用 users/departments 表名。
    __tablename__ = "oa_departments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    parent_id: Mapped[str] = mapped_column(String(36), ForeignKey("oa_departments.id"), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 这是部门可新建账号可选的角色白名单，不是人员实际拥有的权限。
    roles: Mapped[list[Role]] = relationship(secondary=department_roles, lazy="selectin")


class Personnel(Base):
    """人员档案不等同于后台登录账号，避免录入员工时扩大登录面。"""

    __tablename__ = "oa_personnel"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80), index=True)
    phone: Mapped[str] = mapped_column(String(32), default="")
    email: Mapped[str] = mapped_column(String(254), default="")
    gender: Mapped[str] = mapped_column(String(16), default="unspecified")
    department_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("oa_departments.id"), index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("oa_users.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(Base):
    __tablename__ = "oa_users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(80))
    department_id: Mapped[str] = mapped_column(String(36), ForeignKey("oa_departments.id"), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    roles: Mapped[list[Role]] = relationship(secondary=user_roles, lazy="selectin")
    permission_grants: Mapped[list[Permission]] = relationship(
        secondary=user_permission_grants, lazy="selectin"
    )


class Role(Base):
    __tablename__ = "oa_roles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    data_scope: Mapped[str] = mapped_column(String(32), default="self")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    permissions: Mapped[list[Permission]] = relationship(
        secondary=role_permissions, lazy="selectin"
    )


class Permission(Base):
    __tablename__ = "oa_permissions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefreshSession(Base):
    __tablename__ = "oa_refresh_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("oa_users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OaAuditLog(Base):
    __tablename__ = "oa_audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("oa_users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    outcome: Mapped[str] = mapped_column(String(24))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class CatalogProduct(Base):
    """商品运营的可追溯主数据，不复用 Agent 的演练商品 Fixture。"""

    __tablename__ = "catalog_products"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    sku_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(160))
    spec: Mapped[str] = mapped_column(String(120), default="")
    price_fen: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, default="")
    manual: Mapped[str] = mapped_column(Text, default="")
    ingredients: Mapped[list[str]] = mapped_column(JSON, default=list)
    ingredient_disclosure_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    usage: Mapped[str] = mapped_column(Text, default="")
    cautions: Mapped[str] = mapped_column(Text, default="")
    category_code: Mapped[str] = mapped_column(String(64), default="")
    scenario_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    on_sale: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    assistant_approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("oa_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    images: Mapped[list[CatalogProductImage]] = relationship(
        back_populates="product", lazy="selectin", order_by="CatalogProductImage.sort_order"
    )


class CatalogProductImage(Base):
    """图片元数据只保留对象键，原文件名不作为可访问路径。"""

    __tablename__ = "catalog_product_images"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("catalog_products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("oa_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    content_type: Mapped[str] = mapped_column(String(32))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    product: Mapped[CatalogProduct | None] = relationship(back_populates="images")
