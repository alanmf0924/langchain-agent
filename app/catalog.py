from __future__ import annotations

from collections.abc import Iterable

from app.database_catalog import (
    DatabaseCatalogRepository,
    build_database_catalog_repository_from_env,
)
from app.mall_catalog import MallCatalogGateway, build_mall_catalog_gateway_from_env
from app.mock_data import DOCUMENTS, PRODUCTS
from app.models import Evidence, Product


class Catalog:
    """业务数据访问层；生产环境替换为商品、库存与知识库适配器。"""

    def __init__(
        self,
        mall_gateway: MallCatalogGateway | None = None,
        database_catalog: DatabaseCatalogRepository | None = None,
    ) -> None:
        # 未设置 CATALOG_SOURCE=mall 时保持明确的本地演练数据；真实源配置不全会在启动时失败。
        self.database_catalog = database_catalog or build_database_catalog_repository_from_env()
        self.mall_gateway = mall_gateway or build_mall_catalog_gateway_from_env()
        if self.database_catalog is not None and self.mall_gateway is not None:
            raise ValueError("CATALOG_SOURCE 只能选择一种商品读模型")
        self.products = (
            {} if self.database_catalog is not None or self.mall_gateway is not None
            # 商品后台编辑和测试中的临时变更不能污染模块级演练资料，否则后续
            # Catalog 会读取到上一请求留下的价格、库存或成分状态。
            else {product.sku_id: product.model_copy(deep=True) for product in PRODUCTS}
        )

    def search_available(self, question: str, request_id: str = "") -> list[Product]:
        """按肤感与诉求计算可解释的候选权重；生产环境替换为带审核过滤的检索服务。"""
        if self.database_catalog is not None:
            return self.database_catalog.search_available(question)
        if self.mall_gateway is not None:
            return self.mall_gateway.search_available(question, request_id)
        normalized = question.lower()
        keywords = (
            "干", "紧绷", "保湿", "修护", "换季", "泛红", "敏感", "美白", "焕亮", "暗沉",
            "油", "油光", "出油", "毛孔", "控油", "闷",
        )
        has_direct_product_name = any(
            product.name.lower() in normalized for product in self.products.values()
        )
        if not any(word in normalized for word in keywords) and not has_direct_product_name:
            return []
        tag_weights = {
            "干": {"干燥感": 5, "紧绷感": 4, "保湿": 4, "屏障修护": 5, "温和清洁": 2, "敏感倾向": 3},
            "紧绷": {"紧绷感": 6, "屏障修护": 4, "保湿": 3, "温和清洁": 2},
            "紧": {"紧绷感": 5, "屏障修护": 3, "保湿": 2},
            "保湿": {"保湿": 6, "干燥感": 4, "屏障修护": 3, "轻盈保湿": 3},
            "修护": {"屏障修护": 6, "干燥感": 3, "敏感倾向": 3},
            "换季": {"屏障修护": 4, "干燥感": 4, "敏感倾向": 3},
            "泛红": {"屏障修护": 5, "敏感倾向": 5, "温和清洁": 3, "保湿": 2},
            "敏感": {"敏感倾向": 6, "温和清洁": 4, "屏障修护": 4},
            "油": {"控油": 5, "油光": 5, "毛孔": 3, "轻盈保湿": 2},
            "油光": {"油光": 6, "控油": 5, "轻盈保湿": 2},
            "出油": {"控油": 6, "油光": 5, "轻盈保湿": 2},
            "毛孔": {"毛孔": 6, "控油": 3, "轻盈保湿": 2},
            "控油": {"控油": 6, "油光": 4, "轻盈保湿": 2},
            "暗沉": {"暗沉": 6, "焕亮": 5, "均匀肤色": 4},
            "焕亮": {"焕亮": 6, "暗沉": 4, "均匀肤色": 4},
            "提亮": {"焕亮": 6, "暗沉": 4, "均匀肤色": 4},
            "美白": {"焕亮": 6, "暗沉": 4, "均匀肤色": 4},
        }
        scores: list[tuple[int, Product]] = []
        for item in self.products.values():
            if not item.approved or not item.on_sale:
                continue
            score = 20 if item.name.lower() in normalized else 0
            for signal, weights in tag_weights.items():
                if signal in normalized:
                    score += sum(weights.get(tag, 0) for tag in item.tags)
            # 每个完整套餐需要一个温和清洁步骤；它只作为低权重基础项，不会压过针对性产品。
            # 直接点名某个商品时，不能为了凑套餐混入无关的洁面；
            # 否则被点名商品缺货时会错误地转推别的 SKU。
            if score == 0 and "温和清洁" in item.tags and not has_direct_product_name:
                score = 1
            if score > 0:
                scores.append((score, item))
        return [item for _, item in sorted(scores, key=lambda pair: (-pair[0], -pair[1].stock, pair[1].sku_id))]

    def get_realtime_available(
        self, sku_ids: Iterable[str], request_id: str = ""
    ) -> list[Product]:
        """模拟权威商品服务：下架、未审核或零库存 SKU 不会返回。"""
        if self.database_catalog is not None:
            return self.database_catalog.get_realtime_available(list(sku_ids))
        if self.mall_gateway is not None:
            return self.mall_gateway.get_realtime_available(list(sku_ids), request_id)
        return [
            item
            for sku_id in sku_ids
            if (item := self.products.get(sku_id))
            and item.approved
            and item.on_sale
            and item.stock > 0
        ]

    def list_visible(self) -> list[Product]:
        """公开目录仅展示上架商品；库存为零仍可保留缺货展示。"""
        if self.database_catalog is not None:
            return self.database_catalog.list_visible()
        if self.mall_gateway is not None:
            # 外部商城读契约只定义推荐候选和批量实时可售性，不能伪造全量展示目录。
            return []
        return [item for item in self.products.values() if item.on_sale]

    def get_visible_by_sku(self, sku_id: str) -> Product | None:
        """完整资料只允许读取当前上架商品，避免下架后仍可通过旧链接访问。"""
        if self.database_catalog is not None:
            return self.database_catalog.get_visible_by_sku(sku_id)
        if self.mall_gateway is not None:
            return None
        product = self.products.get(sku_id)
        return product if product is not None and product.on_sale else None

    def evidence_for(self, sku_ids: Iterable[str]) -> list[Evidence]:
        """只返回与已选 SKU 对应、审核通过且在售的资料片段。"""
        if self.database_catalog is not None:
            return self.database_catalog.evidence_for(list(sku_ids))
        if self.mall_gateway is not None:
            # 外部商城契约尚未定义可引用的商品资料，不得借用本地演练资料伪造证据。
            return []
        selected = set(sku_ids)
        return [
            Evidence(
                id=item.id,
                sku_id=item.sku_id,
                title=item.title,
                version=item.version,
                quote=item.content,
            )
            for item in DOCUMENTS
            if item.sku_id in selected and item.approved and item.on_sale
        ]
