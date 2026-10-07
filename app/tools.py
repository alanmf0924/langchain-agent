from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from app.catalog import Catalog
from app.knowledge_base import KnowledgeBase
from app.policy import decide


def build_read_tools(catalog: Catalog, knowledge_base: KnowledgeBase) -> list[BaseTool]:
    """只将 policy 已允许的只读操作暴露给 LangChain。"""

    @tool
    def search_catalog(question: str, request_id: str) -> list[dict]:
        """查找已审核且上架的、与用户肤况相关的商品候选。"""
        # 即使调用路径意外绕过 Service，Tool 自身仍不会在未授权时读取数据。
        if decide("search_catalog").decision != "allow":
            return []
        return [item.model_dump() for item in catalog.search_available(question, request_id)]

    @tool
    def get_realtime_price_stock(sku_ids: list[str], request_id: str) -> list[dict]:
        """查询给定 SKU 的当前上架、库存与价格；不可售商品不会返回。"""
        if decide("get_realtime_price_stock").decision != "allow":
            return []
        return [item.model_dump() for item in catalog.get_realtime_available(sku_ids, request_id)]

    @tool
    def retrieve_evidence(question: str, sku_ids: list[str]) -> list[dict]:
        """从审核、上架且 SKU 匹配的知识库资料中检索可引用证据。"""
        if decide("retrieve_evidence").decision != "allow":
            return []
        knowledge_evidence = knowledge_base.search(question, sku_ids)
        # 数据库商品刚审核上线、知识库资料尚未单独发布时，使用同一商品记录中
        # 已审核的用法/注意事项生成带 revision 的最小证据，绝不退回 Mock 文档。
        evidence = knowledge_evidence or catalog.evidence_for(sku_ids)
        return [item.model_dump() for item in evidence]

    return [search_catalog, get_realtime_price_stock, retrieve_evidence]
