"""商城运营接口：OA 仅凭明确权限查看客户与处理订单。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.customer.models import Customer, CustomerOrder, CustomerOrderItem
from app.oa.api_contract import OaApiRoute
from app.oa.database import get_session
from app.oa.dependencies import require_permission
from app.oa.models import CatalogProduct, User
from app.oa.schemas import PageResult
from app.oa.services import write_audit_log

router = APIRouter(prefix="/api/v1/commerce", tags=["商城运营"], route_class=OaApiRoute)

OrderStatus = Literal["pending_payment", "paid", "fulfilling", "shipped", "completed", "cancelled"]
ORDER_STATUSES: tuple[OrderStatus, ...] = (
    "pending_payment",
    "paid",
    "fulfilling",
    "shipped",
    "completed",
    "cancelled",
)
ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    "pending_payment": {"paid", "cancelled"},
    "paid": {"fulfilling", "cancelled"},
    "fulfilling": {"shipped", "cancelled"},
    "shipped": {"completed"},
    "completed": set(),
    "cancelled": set(),
}


class CustomerListItem(BaseModel):
    id: str
    username: str
    display_name: str
    email: str
    email_verified: bool
    is_active: bool
    created_at: datetime
    order_count: int
    order_total_fen: int


class OrderLineItem(BaseModel):
    product_id: str
    sku_id: str
    product_title: str
    product_spec: str
    unit_price_fen: int
    quantity: int
    subtotal_fen: int


class OrderAdminItem(BaseModel):
    id: str
    order_no: str
    status: OrderStatus
    total_fen: int
    customer_id: str
    customer_username: str
    customer_display_name: str
    customer_email: str
    recipient_name: str
    recipient_phone: str
    recipient_province: str
    recipient_city: str
    recipient_district: str
    recipient_detail: str
    recipient_postal_code: str
    customer_remark: str
    fulfillment_note: str
    tracking_no: str
    paid_at: datetime | None
    shipped_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
    items: list[OrderLineItem]


class OrderStatusUpdate(BaseModel):
    expected_status: OrderStatus
    status: OrderStatus
    fulfillment_note: str | None = Field(default=None, max_length=1000)
    tracking_no: str | None = Field(default=None, max_length=120)


def order_status(order: CustomerOrder) -> OrderStatus:
    if order.status not in ORDER_STATUSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="订单状态数据无效")
    return cast(OrderStatus, order.status)


async def order_item(
    session: AsyncSession, order: CustomerOrder, customer: Customer
) -> OrderAdminItem:
    lines = (
        await session.scalars(
            select(CustomerOrderItem)
            .where(CustomerOrderItem.order_id == order.id)
            .order_by(CustomerOrderItem.created_at, CustomerOrderItem.id)
        )
    ).all()
    return OrderAdminItem(
        id=order.id,
        order_no=order.order_no,
        status=order_status(order),
        total_fen=order.total_fen,
        customer_id=customer.id,
        customer_username=customer.username,
        customer_display_name=customer.display_name,
        customer_email=customer.email or "",
        recipient_name=order.recipient_name,
        recipient_phone=order.recipient_phone,
        recipient_province=order.recipient_province,
        recipient_city=order.recipient_city,
        recipient_district=order.recipient_district,
        recipient_detail=order.recipient_detail,
        recipient_postal_code=order.recipient_postal_code,
        customer_remark=order.customer_remark,
        fulfillment_note=order.fulfillment_note,
        tracking_no=order.tracking_no,
        paid_at=order.paid_at,
        shipped_at=order.shipped_at,
        cancelled_at=order.cancelled_at,
        created_at=order.created_at,
        items=[
            OrderLineItem(
                product_id=line.product_id,
                sku_id=line.sku_id,
                product_title=line.product_title,
                product_spec=line.product_spec,
                unit_price_fen=line.unit_price_fen,
                quantity=line.quantity,
                subtotal_fen=line.subtotal_fen,
            )
            for line in lines
        ],
    )


async def order_and_customer_or_404(
    session: AsyncSession, order_id: str, *, lock: bool = False
) -> tuple[CustomerOrder, Customer]:
    statement = (
        select(CustomerOrder, Customer)
        .join(Customer, Customer.id == CustomerOrder.customer_id)
        .where(CustomerOrder.id == order_id)
    )
    if lock:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="订单不存在")
    return row


@router.get("/customers", response_model=PageResult[CustomerListItem], summary="分页列出前台客户")
async def list_customers(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str = Query(default="", max_length=120),
    _: User = Depends(require_permission("customer:profile:read")),
    session: AsyncSession = Depends(get_session),
) -> PageResult[CustomerListItem]:
    normalized = keyword.strip()
    predicates = []
    if normalized:
        pattern = f"%{normalized}%"
        predicates.append(
            or_(
                Customer.username.ilike(pattern),
                Customer.display_name.ilike(pattern),
                Customer.email.ilike(pattern),
            )
        )
    total = await session.scalar(select(func.count()).select_from(Customer).where(*predicates)) or 0
    rows = (
        await session.execute(
            select(
                Customer,
                func.count(CustomerOrder.id).label("order_count"),
                func.coalesce(func.sum(CustomerOrder.total_fen), 0).label("order_total_fen"),
            )
            .outerjoin(CustomerOrder, CustomerOrder.customer_id == Customer.id)
            .where(*predicates)
            .group_by(Customer.id)
            .order_by(Customer.created_at.desc(), Customer.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return PageResult(
        items=[
            CustomerListItem(
                id=customer.id,
                username=customer.username,
                display_name=customer.display_name,
                email=customer.email or "",
                email_verified=customer.email_verified,
                is_active=customer.is_active,
                created_at=customer.created_at,
                order_count=int(order_count),
                order_total_fen=int(order_total_fen),
            )
            for customer, order_count, order_total_fen in rows
        ],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/orders", response_model=PageResult[OrderAdminItem], summary="分页列出商城订单")
async def list_orders(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str = Query(default="", max_length=120),
    order_status_filter: OrderStatus | None = Query(default=None, alias="status"),
    _: User = Depends(require_permission("order:record:read")),
    session: AsyncSession = Depends(get_session),
) -> PageResult[OrderAdminItem]:
    predicates = []
    if order_status_filter is not None:
        predicates.append(CustomerOrder.status == order_status_filter)
    normalized = keyword.strip()
    if normalized:
        pattern = f"%{normalized}%"
        predicates.append(
            or_(
                CustomerOrder.order_no.ilike(pattern),
                Customer.username.ilike(pattern),
                Customer.display_name.ilike(pattern),
                Customer.email.ilike(pattern),
            )
        )
    total = (
        await session.scalar(
            select(func.count())
            .select_from(CustomerOrder)
            .join(Customer, Customer.id == CustomerOrder.customer_id)
            .where(*predicates)
        )
    ) or 0
    rows = (
        await session.execute(
            select(CustomerOrder, Customer)
            .join(Customer, Customer.id == CustomerOrder.customer_id)
            .where(*predicates)
            .order_by(CustomerOrder.created_at.desc(), CustomerOrder.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return PageResult(
        items=[await order_item(session, order, customer) for order, customer in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/orders/{order_id}", response_model=OrderAdminItem, summary="查看商城订单详情")
async def get_order(
    order_id: str,
    _: User = Depends(require_permission("order:record:read")),
    session: AsyncSession = Depends(get_session),
) -> OrderAdminItem:
    order, customer = await order_and_customer_or_404(session, order_id)
    return await order_item(session, order, customer)


async def lock_products_for_lines(
    session: AsyncSession, lines: list[CustomerOrderItem]
) -> dict[str, CatalogProduct]:
    product_ids = sorted({line.product_id for line in lines})
    products = (
        await session.scalars(
            select(CatalogProduct).where(CatalogProduct.id.in_(product_ids)).with_for_update()
        )
    ).all()
    by_id = {product.id: product for product in products}
    if len(by_id) != len(product_ids):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="订单商品不存在，需人工核对库存"
        )
    return by_id


@router.patch(
    "/orders/{order_id}/status", response_model=OrderAdminItem, summary="按状态机处理商城订单"
)
async def update_order_status(
    order_id: str,
    payload: OrderStatusUpdate,
    actor: User = Depends(require_permission("order:record:write")),
    session: AsyncSession = Depends(get_session),
) -> OrderAdminItem:
    order, customer = await order_and_customer_or_404(session, order_id, lock=True)
    current = order_status(order)
    if current != payload.expected_status:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="订单状态已变化，请刷新后重试"
        )
    if payload.status not in ALLOWED_TRANSITIONS[current]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="不允许的订单状态流转")

    lines = (
        await session.scalars(
            select(CustomerOrderItem).where(CustomerOrderItem.order_id == order.id)
        )
    ).all()
    now = datetime.now(UTC)
    if payload.status == "paid":
        products = await lock_products_for_lines(session, lines)
        for line in lines:
            product = products[line.product_id]
            if not product.on_sale or product.stock < line.quantity:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="商品已下架或库存不足"
                )
        for line in lines:
            products[line.product_id].stock -= line.quantity
        order.paid_at = now
    elif payload.status == "cancelled" and current == "paid":
        products = await lock_products_for_lines(session, lines)
        for line in lines:
            products[line.product_id].stock += line.quantity
        order.cancelled_at = now
    elif payload.status == "cancelled":
        order.cancelled_at = now
    elif payload.status == "shipped":
        next_tracking = (
            payload.tracking_no.strip() if payload.tracking_no is not None else order.tracking_no
        )
        if not next_tracking:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="发货时必须填写物流单号"
            )
        order.tracking_no = next_tracking
        order.shipped_at = now

    if payload.fulfillment_note is not None:
        order.fulfillment_note = payload.fulfillment_note.strip()
    if payload.tracking_no is not None and payload.status != "shipped":
        order.tracking_no = payload.tracking_no.strip()
    order.status = payload.status
    await write_audit_log(
        session,
        actor_id=actor.id,
        action="commerce.order.status.update",
        outcome="success",
        detail=f"{order.id}:{current}->{payload.status}",
    )
    await session.commit()
    await session.refresh(order)
    return await order_item(session, order, customer)
