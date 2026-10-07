"""由 OA 商品后台维护的本地商品读模型，供 Agent 和公开目录使用。"""

from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import create_database_engine
from app.models import Evidence, Product
from app.oa.models import CatalogProduct


def product_image_url(storage_key: str) -> str:
    """返回由后端公开路由托管的商品图，不暴露存储目录或原始文件名。"""
    return f"/uploads/products/{storage_key}"


class DatabaseCatalogError(RuntimeError):
    """数据库商品读模型不可用时的稳定错误分类。"""


class DatabaseCatalogRepository:
    def __init__(self) -> None:
        self._engine = create_database_engine()

    @staticmethod
    def _product(item: CatalogProduct) -> Product:
        return Product(
            sku_id=item.sku_id,
            name=item.title,
            spec=item.spec,
            price_fen=item.price_fen,
            stock=item.stock,
            on_sale=item.on_sale,
            approved=item.assistant_approved,
            tags=list(item.scenario_tags or []),
            ingredients=list(item.ingredients or []),
            ingredient_disclosure_complete=item.ingredient_disclosure_complete,
            usage=item.usage,
            cautions=item.cautions,
            image_urls=[
                product_image_url(image.storage_key)
                for image in sorted(item.images, key=lambda image: (image.sort_order, image.id))
            ],
        )

    def _eligible_products(self) -> list[CatalogProduct]:
        try:
            with Session(self._engine) as session:
                return list(
                    session.scalars(
                        select(CatalogProduct)
                        .options(selectinload(CatalogProduct.images))
                        .where(
                            CatalogProduct.on_sale.is_(True),
                            CatalogProduct.assistant_approved.is_(True),
                        )
                        .order_by(CatalogProduct.sku_id)
                    )
                )
        except Exception as error:  # Schema/connectivity faults must never fall back to mock data.
            raise DatabaseCatalogError("database_catalog_unavailable") from error

    def list_visible(self) -> list[Product]:
        return [self._product(item) for item in self._eligible_products()]

    def search_available(self, question: str) -> list[Product]:
        normalized = question.lower()
        scored: list[tuple[int, Product]] = []
        for item in self._eligible_products():
            product = self._product(item)
            haystack = " ".join((product.name, *product.tags)).lower()
            score = sum(3 for tag in product.tags if tag.lower() in normalized)
            score += 20 if product.name.lower() in normalized else 0
            score += sum(1 for word in ("干", "油", "敏感", "修护", "保湿", "毛孔", "暗沉", "焕亮") if word in normalized and word in haystack)
            if score:
                scored.append((score, product))
        return [product for _, product in sorted(scored, key=lambda pair: (-pair[0], -pair[1].stock, pair[1].sku_id))]

    def get_realtime_available(self, sku_ids: list[str]) -> list[Product]:
        requested = list(dict.fromkeys(sku_ids))
        if not requested:
            return []
        try:
            with Session(self._engine) as session:
                rows = session.scalars(
                    select(CatalogProduct)
                    .options(selectinload(CatalogProduct.images))
                    .where(
                        CatalogProduct.sku_id.in_(requested),
                        CatalogProduct.on_sale.is_(True),
                        CatalogProduct.assistant_approved.is_(True),
                        CatalogProduct.stock > 0,
                    )
                )
                by_sku = {item.sku_id: self._product(item) for item in rows}
        except Exception as error:
            raise DatabaseCatalogError("database_catalog_unavailable") from error
        return [by_sku[sku_id] for sku_id in requested if sku_id in by_sku]

    def evidence_for(self, sku_ids: list[str]) -> list[Evidence]:
        """将已审核、在售商品的后台资料以带版本的最小证据形式公开给 Agent。

        商品后台资料不是营销文案的替代物：这里仅引用运营人员录入并审核过的
        规格、适用场景、用法与注意事项。成分完整性仍由 Product 字段和业务策略
        单独判断，不能因为存在这条资料而推断可做成分回避承诺。
        """
        requested = list(dict.fromkeys(sku_ids))
        if not requested:
            return []
        try:
            with Session(self._engine) as session:
                rows = session.scalars(
                    select(CatalogProduct).where(
                        CatalogProduct.sku_id.in_(requested),
                        CatalogProduct.on_sale.is_(True),
                        CatalogProduct.assistant_approved.is_(True),
                    )
                )
                by_sku = {item.sku_id: item for item in rows}
        except Exception as error:
            raise DatabaseCatalogError("database_catalog_unavailable") from error

        evidence: list[Evidence] = []
        for sku_id in requested:
            item = by_sku.get(sku_id)
            if item is None:
                continue
            evidence.append(
                Evidence(
                    id=f"catalog:{item.id}:r{item.revision}",
                    sku_id=item.sku_id,
                    title=f"{item.title} 商品后台资料",
                    version=f"catalog-r{item.revision}",
                    quote=(
                        f"规格：{item.spec or '未填写'}；"
                        f"适用场景：{'、'.join(item.scenario_tags or []) or '未填写'}；"
                        f"使用方法：{item.usage or '未填写'}；"
                        f"注意事项：{item.cautions or '未填写'}"
                    ),
                    source_chunk_id=f"catalog-product:{item.id}:r{item.revision}",
                )
            )
        return evidence


def build_database_catalog_repository_from_env() -> DatabaseCatalogRepository | None:
    source = os.getenv("CATALOG_SOURCE", "mock").lower()
    if source == "database":
        return DatabaseCatalogRepository()
    return None
