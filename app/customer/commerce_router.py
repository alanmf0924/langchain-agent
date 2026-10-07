"""前台下单接口：所有资源均以当前 customer_id 为归属边界。"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.customer.database import get_session
from app.customer.dependencies import get_current_customer
from app.customer.models import (
    Customer,
    CustomerAddress,
    CustomerOrder,
    CustomerOrderItem,
    new_customer_id,
)
from app.oa.models import CatalogProduct

router = APIRouter(prefix="/api/customer", tags=["前台订单"])

OrderStatus = Literal["pending_payment", "paid", "fulfilling", "shipped", "completed", "cancelled"]


def clean_required(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("不能为空")
    return normalized


class AddressCreate(BaseModel):
    recipient_name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=3, max_length=32)
    province: str = Field(default="", max_length=64)
    city: str = Field(default="", max_length=64)
    district: str = Field(default="", max_length=64)
    detail: str = Field(min_length=1, max_length=255)
    postal_code: str = Field(default="", max_length=20)
    is_default: bool = False

    _clean_recipient = field_validator("recipient_name")(clean_required)
    _clean_phone = field_validator("phone")(clean_required)
    _clean_detail = field_validator("detail")(clean_required)


class AddressResponse(BaseModel):
    id: str
    recipient_name: str
    phone: str
    province: str
    city: str
    district: str
    detail: str
    postal_code: str
    is_default: bool


class OrderCreateItem(BaseModel):
    product_id: str = Field(min_length=1, max_length=36)
    quantity: int = Field(ge=1, le=100)


class OrderCreate(BaseModel):
    address_id: str = Field(min_length=1, max_length=36)
    items: list[OrderCreateItem] = Field(min_length=1, max_length=30)
    customer_remark: str = Field(default="", max_length=1000)


class OrderItemResponse(BaseModel):
    product_id: str
    sku_id: str
    product_title: str
    product_spec: str
    unit_price_fen: int
    quantity: int
    subtotal_fen: int


class OrderResponse(BaseModel):
    id: str
    order_no: str
    status: OrderStatus
    total_fen: int
    customer_remark: str
    fulfillment_note: str
    tracking_no: str
    recipient_name: str
    recipient_phone: str
    recipient_province: str
    recipient_city: str
    recipient_district: str
    recipient_detail: str
    recipient_postal_code: str
    paid_at: datetime | None
    shipped_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
    items: list[OrderItemResponse]


def address_response(address: CustomerAddress) -> AddressResponse:
    return AddressResponse(
        id=address.id,
        recipient_name=address.recipient_name,
        phone=address.phone,
        province=address.province,
        city=address.city,
        district=address.district,
        detail=address.detail,
        postal_code=address.postal_code,
        is_default=address.is_default,
    )


async def order_response(session: AsyncSession, order: CustomerOrder) -> OrderResponse:
    rows = (
        await session.scalars(
            select(CustomerOrderItem)
            .where(CustomerOrderItem.order_id == order.id)
            .order_by(CustomerOrderItem.created_at, CustomerOrderItem.id)
        )
    ).all()
    return OrderResponse(
        id=order.id,
        order_no=order.order_no,
        status=order.status,  # type: ignore[arg-type]
        total_fen=order.total_fen,
        customer_remark=order.customer_remark,
        fulfillment_note=order.fulfillment_note,
        tracking_no=order.tracking_no,
        recipient_name=order.recipient_name,
        recipient_phone=order.recipient_phone,
        recipient_province=order.recipient_province,
        recipient_city=order.recipient_city,
        recipient_district=order.recipient_district,
        recipient_detail=order.recipient_detail,
        recipient_postal_code=order.recipient_postal_code,
        paid_at=order.paid_at,
        shipped_at=order.shipped_at,
        cancelled_at=order.cancelled_at,
        created_at=order.created_at,
        items=[
            OrderItemResponse(
                product_id=item.product_id,
                sku_id=item.sku_id,
                product_title=item.product_title,
                product_spec=item.product_spec,
                unit_price_fen=item.unit_price_fen,
                quantity=item.quantity,
                subtotal_fen=item.subtotal_fen,
            )
            for item in rows
        ],
    )


@router.get("/addresses", response_model=list[AddressResponse], summary="列出我的收货地址")
async def list_addresses(
    customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> list[AddressResponse]:
    addresses = (
        await session.scalars(
            select(CustomerAddress)
            .where(CustomerAddress.customer_id == customer.id)
            .order_by(CustomerAddress.is_default.desc(), CustomerAddress.updated_at.desc())
        )
    ).all()
    return [address_response(address) for address in addresses]


@router.post(
    "/addresses",
    response_model=AddressResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新增我的收货地址",
)
async def create_address(
    payload: AddressCreate,
    customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> AddressResponse:
    if payload.is_default:
        await session.execute(
            update(CustomerAddress)
            .where(CustomerAddress.customer_id == customer.id)
            .values(is_default=False)
        )
    address = CustomerAddress(id=new_customer_id(), customer_id=customer.id, **payload.model_dump())
    session.add(address)
    await session.commit()
    await session.refresh(address)
    return address_response(address)


@router.post(
    "/orders",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建待支付订单",
)
async def create_order(
    payload: OrderCreate,
    customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> OrderResponse:
    address = await session.scalar(
        select(CustomerAddress).where(
            CustomerAddress.id == payload.address_id,
            CustomerAddress.customer_id == customer.id,
        )
    )
    if address is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="收货地址不存在")

    quantities = Counter({item.product_id: 0 for item in payload.items})
    for item in payload.items:
        quantities[item.product_id] += item.quantity
    if any(quantity > 100 for quantity in quantities.values()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="单个商品数量不能超过 100"
        )

    products = (
        await session.scalars(
            select(CatalogProduct).where(CatalogProduct.id.in_(sorted(quantities)))
        )
    ).all()
    by_id = {product.id: product for product in products}
    unavailable = [
        product_id
        for product_id in quantities
        if product_id not in by_id or not by_id[product_id].on_sale
    ]
    if unavailable:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="商品不存在或已下架")

    order_id = new_customer_id()
    order = CustomerOrder(
        id=order_id,
        order_no=f"CO{datetime.now(UTC):%Y%m%d}{order_id.replace('-', '')[:10].upper()}",
        customer_id=customer.id,
        status="pending_payment",
        total_fen=sum(
            by_id[product_id].price_fen * quantity for product_id, quantity in quantities.items()
        ),
        recipient_name=address.recipient_name,
        recipient_phone=address.phone,
        recipient_province=address.province,
        recipient_city=address.city,
        recipient_district=address.district,
        recipient_detail=address.detail,
        recipient_postal_code=address.postal_code,
        customer_remark=payload.customer_remark.strip(),
    )
    session.add(order)
    for product_id, quantity in quantities.items():
        product = by_id[product_id]
        session.add(
            CustomerOrderItem(
                id=new_customer_id(),
                order_id=order_id,
                product_id=product.id,
                sku_id=product.sku_id,
                product_title=product.title,
                product_spec=product.spec,
                unit_price_fen=product.price_fen,
                quantity=quantity,
                subtotal_fen=product.price_fen * quantity,
            )
        )
    await session.commit()
    await session.refresh(order)
    return await order_response(session, order)


@router.get("/orders", response_model=list[OrderResponse], summary="列出我的订单")
async def list_orders(
    limit: int = Query(default=20, ge=1, le=100),
    customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> list[OrderResponse]:
    orders = (
        await session.scalars(
            select(CustomerOrder)
            .where(CustomerOrder.customer_id == customer.id)
            .order_by(CustomerOrder.created_at.desc(), CustomerOrder.id)
            .limit(limit)
        )
    ).all()
    return [await order_response(session, order) for order in orders]


@router.get("/orders/{order_id}", response_model=OrderResponse, summary="查看我的订单详情")
async def get_order(
    order_id: str,
    customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> OrderResponse:
    order = await session.scalar(
        select(CustomerOrder).where(
            CustomerOrder.id == order_id,
            CustomerOrder.customer_id == customer.id,
        )
    )
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="订单不存在")
    return await order_response(session, order)
