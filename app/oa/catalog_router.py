"""商品后台接口：上传图片、维护主数据，并以版本号避免覆盖他人刚提交的改动。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.oa.api_contract import OaApiRoute
from app.oa.database import get_session
from app.oa.dependencies import require_permission
from app.oa.models import CatalogProduct, CatalogProductImage, User, new_id
from app.oa.schemas import (
    CatalogImageItem,
    CatalogProductCreate,
    CatalogProductItem,
    CatalogProductUpdate,
    PageResult,
)
from app.oa.services import data_scope, write_audit_log
from app.product_uploads import delete_image, image_path, save_image

router = APIRouter(prefix="/api/v1/catalog", tags=["商品后台"], route_class=OaApiRoute)
public_router = APIRouter(tags=["商品"])


def image_url(storage_key: str) -> str:
    return f"/uploads/products/{storage_key}"


def product_item(product: CatalogProduct) -> CatalogProductItem:
    images = [
        CatalogImageItem(
            id=image.id,
            url=image_url(image.storage_key),
            content_type=image.content_type,
            size_bytes=image.size_bytes,
            sort_order=image.sort_order,
        )
        for image in sorted(product.images, key=lambda image: (image.sort_order, image.id))
    ]
    return CatalogProductItem(
        id=product.id,
        sku_id=product.sku_id,
        title=product.title,
        spec=product.spec,
        price_fen=product.price_fen,
        stock=product.stock,
        description=product.description,
        manual=product.manual,
        ingredients=list(product.ingredients or []),
        ingredient_disclosure_complete=product.ingredient_disclosure_complete,
        usage=product.usage,
        cautions=product.cautions,
        category_code=product.category_code,
        scenario_tags=list(product.scenario_tags or []),
        on_sale=product.on_sale,
        assistant_approved=product.assistant_approved,
        revision=product.revision,
        image_urls=[image.url for image in images],
        images=images,
    )


def creator_scope_predicate(creator_id_column, actor: User):
    """将角色数据范围落实到由 OA 账号创建的业务数据。"""
    scope = data_scope(actor)
    if scope == "all":
        return True
    if scope == "department":
        return creator_id_column.in_(
            select(User.id).where(User.department_id == actor.department_id)
        )
    return creator_id_column == actor.id


async def product_or_404(session: AsyncSession, product_id: str, actor: User) -> CatalogProduct:
    product = await session.scalar(
        select(CatalogProduct)
        .options(selectinload(CatalogProduct.images))
        .where(
            CatalogProduct.id == product_id,
            creator_scope_predicate(CatalogProduct.created_by, actor),
        )
    )
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="catalog product not found")
    return product


async def resolve_images(
    session: AsyncSession, image_ids: list[str], actor: User, product_id: str | None = None
) -> list[CatalogProductImage]:
    if not image_ids:
        return []
    images = (
        await session.scalars(
            select(CatalogProductImage).where(
                CatalogProductImage.id.in_(image_ids),
                creator_scope_predicate(CatalogProductImage.created_by, actor),
            )
        )
    ).all()
    by_id = {image.id: image for image in images}
    missing = [image_id for image_id in image_ids if image_id not in by_id]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="image does not exist or is outside the data scope",
        )
    occupied = [
        image.id for image in images if image.product_id is not None and image.product_id != product_id
    ]
    if occupied:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="image is already attached")
    return [by_id[image_id] for image_id in image_ids]


async def attach_images(
    session: AsyncSession, product: CatalogProduct, image_ids: list[str], actor: User
) -> None:
    images = await resolve_images(session, image_ids, actor, product.id)
    for image in product.images:
        if image.id not in image_ids:
            image.product_id = None
    for index, image in enumerate(images):
        image.product_id = product.id
        image.sort_order = index


@router.post(
    "/uploads/images",
    response_model=CatalogImageItem,
    status_code=status.HTTP_201_CREATED,
    summary="上传待关联的商品图片",
)
async def upload_product_image(
    file: UploadFile = File(...),
    actor: User = Depends(require_permission("catalog:product:write")),
    session: AsyncSession = Depends(get_session),
) -> CatalogImageItem:
    storage_key, content_type, size_bytes = await save_image(file)
    image = CatalogProductImage(
        id=new_id(),
        storage_key=storage_key,
        created_by=actor.id,
        content_type=content_type,
        size_bytes=size_bytes,
    )
    try:
        session.add(image)
        await write_audit_log(session, actor_id=actor.id, action="catalog.image.upload", outcome="success")
        await session.commit()
    except Exception:
        await session.rollback()
        delete_image(storage_key)
        raise
    return CatalogImageItem(
        id=image.id,
        url=image_url(image.storage_key),
        content_type=image.content_type,
        size_bytes=image.size_bytes,
        sort_order=image.sort_order,
    )


@router.post(
    "/products",
    response_model=CatalogProductItem,
    status_code=status.HTTP_201_CREATED,
    summary="创建商品",
)
async def create_product(
    payload: CatalogProductCreate,
    actor: User = Depends(require_permission("catalog:product:write")),
    session: AsyncSession = Depends(get_session),
) -> CatalogProductItem:
    if await session.scalar(select(CatalogProduct.id).where(CatalogProduct.sku_id == payload.sku_id)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="sku_id already exists")
    images = await resolve_images(session, payload.image_ids, actor)
    values = payload.model_dump(exclude={"image_ids"})
    product = CatalogProduct(id=new_id(), created_by=actor.id, **values)
    session.add(product)
    await session.flush()
    for index, image in enumerate(images):
        image.product_id = product.id
        image.sort_order = index
    await write_audit_log(
        session, actor_id=actor.id, action="catalog.product.create", outcome="success", detail=product.id
    )
    await session.commit()
    return product_item(await product_or_404(session, product.id, actor))


@router.get("/products", response_model=PageResult[CatalogProductItem], summary="分页列出后台商品")
async def list_products(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    actor: User = Depends(require_permission("catalog:product:read")),
    session: AsyncSession = Depends(get_session),
) -> PageResult[CatalogProductItem]:
    scope = creator_scope_predicate(CatalogProduct.created_by, actor)
    total = await session.scalar(select(func.count()).select_from(CatalogProduct).where(scope)) or 0
    products = (
        await session.scalars(
            select(CatalogProduct)
            .options(selectinload(CatalogProduct.images))
            .where(scope)
            .order_by(CatalogProduct.created_at.desc(), CatalogProduct.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return PageResult(items=[product_item(product) for product in products], page=page, page_size=page_size, total=total)


@router.get("/products/{product_id}", response_model=CatalogProductItem, summary="获取后台商品详情")
async def get_product(
    product_id: str,
    actor: User = Depends(require_permission("catalog:product:read")),
    session: AsyncSession = Depends(get_session),
) -> CatalogProductItem:
    return product_item(await product_or_404(session, product_id, actor))


@router.patch("/products/{product_id}", response_model=CatalogProductItem, summary="更新商品")
async def update_product(
    product_id: str,
    payload: CatalogProductUpdate,
    actor: User = Depends(require_permission("catalog:product:write")),
    session: AsyncSession = Depends(get_session),
) -> CatalogProductItem:
    product = await product_or_404(session, product_id, actor)
    if product.revision != payload.expected_revision:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="product revision conflict")
    values = payload.model_dump(exclude_unset=True, exclude={"expected_revision", "image_ids"})
    for field, value in values.items():
        setattr(product, field, value)
    if payload.image_ids is not None:
        await attach_images(session, product, payload.image_ids, actor)
    product.revision += 1
    await write_audit_log(
        session, actor_id=actor.id, action="catalog.product.update", outcome="success", detail=product.id
    )
    await session.commit()
    # attach_images updates image.product_id directly.  The already-loaded
    # product.images collection is therefore stale until it is expired; without
    # this, the write succeeds but the PATCH response incorrectly reports no images.
    session.expire(product, ["images"])
    return product_item(await product_or_404(session, product.id, actor))


@public_router.get("/uploads/products/{storage_key}", include_in_schema=False)
async def get_product_image(storage_key: str) -> FileResponse:
    path = image_path(storage_key)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product image not found")
    return FileResponse(path)
