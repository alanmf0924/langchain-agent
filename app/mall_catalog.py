"""商城商品/库存只读适配器；未显式配置时绝不替换本地演练数据。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Protocol

import httpx

from app.models import Product


class MallCatalogError(RuntimeError):
    """对上游错误的脱敏分类，调用方不应把网关细节暴露给 SSE 或用户。"""


class AccessTokenProvider(Protocol):
    def get_access_token(self) -> str: ...


class OAuthClientCredentialsTokenProvider:
    """仅缓存短期服务 Token；客户端密钥只经部署 Secret 注入，不写入业务审计。"""

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str,
        client: httpx.Client,
        timeout_seconds: int,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._access_token = ""
        self._expires_at = datetime.min.replace(tzinfo=UTC)

    def get_access_token(self) -> str:
        if self._access_token and self._expires_at > datetime.now(UTC):
            return self._access_token
        try:
            response = self._client.post(
                self._token_url,
                data={"grant_type": "client_credentials", "scope": self._scope},
                auth=(self._client_id, self._client_secret),
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise MallCatalogError("mall_auth_unavailable") from error
        token = payload.get("access_token") if isinstance(payload, dict) else None
        expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token or not isinstance(expires_in, int) or expires_in < 60:
            raise MallCatalogError("mall_auth_invalid_response")
        self._access_token = token
        # 提前 30 秒刷新，避免请求途中因临界过期被拒绝。
        self._expires_at = datetime.now(UTC) + timedelta(seconds=max(30, expires_in - 30))
        return token


class MallCatalogGateway:
    """实现已确认草案的候选和强校验读接口，返回现有 Catalog 可消费的 Product 快照。"""

    _CATEGORY_CODES: ClassVar[tuple[str, ...]] = (
        "FACE_CLEANSER",
        "FACE_SERUM",
        "FACE_CREAM",
        "FACE_LOTION",
    )
    _TAG_SIGNALS: ClassVar[dict[str, tuple[str, ...]]] = {
        "干": ("干燥感", "保湿", "屏障修护"),
        "紧绷": ("紧绷感", "屏障修护"),
        "修护": ("屏障修护",),
        "泛红": ("敏感倾向", "屏障修护"),
        "敏感": ("敏感倾向",),
        "油": ("控油", "油光"),
        "毛孔": ("毛孔", "控油"),
        "暗沉": ("焕亮", "均匀肤色"),
        "焕亮": ("焕亮",),
        "美白": ("焕亮",),
    }

    def __init__(
        self, base_url: str, token_provider: AccessTokenProvider, client: httpx.Client, timeout_seconds: int
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._candidate_cache: dict[str, Product] = {}

    @classmethod
    def scenario_tags(cls, question: str) -> list[str]:
        """只发送确定性场景标签，禁止把用户完整描述传给商城读服务。"""
        normalized = question.lower()
        tags = {tag for signal, values in cls._TAG_SIGNALS.items() if signal in normalized for tag in values}
        return sorted(tags)[:8]

    def _post(self, path: str, request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not request_id:
            raise MallCatalogError("mall_catalog_missing_request_id")
        try:
            response = self._client.post(
                f"{self._base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self._token_provider.get_access_token()}",
                    "X-Request-ID": request_id,
                },
                json=payload,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except MallCatalogError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise MallCatalogError("mall_catalog_unavailable") from error
        if not isinstance(body, dict):
            raise MallCatalogError("mall_catalog_invalid_response")
        return body

    @staticmethod
    def _required_text(payload: dict[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise MallCatalogError(f"mall_catalog_invalid_{field}")
        return value

    @classmethod
    def _product_from_payload(cls, payload: dict[str, Any], available_quantity: int) -> Product:
        price_fen = payload.get("sale_price_fen")
        tags = payload.get("scenario_tags")
        ingredients = payload.get("ingredients")
        if isinstance(price_fen, bool) or not isinstance(price_fen, int) or price_fen < 0:
            raise MallCatalogError("mall_catalog_invalid_sale_price_fen")
        if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
            raise MallCatalogError("mall_catalog_invalid_scenario_tags")
        if not isinstance(ingredients, list) or not all(isinstance(item, str) for item in ingredients):
            raise MallCatalogError("mall_catalog_invalid_ingredients")
        if payload.get("assistant_approved") is not True or payload.get("on_sale") is not True:
            raise MallCatalogError("mall_catalog_ineligible_product")
        return Product(
            sku_id=cls._required_text(payload, "sku_id"),
            name=cls._required_text(payload, "name"),
            spec=cls._required_text(payload, "spec"),
            price_fen=price_fen,
            stock=available_quantity,
            on_sale=True,
            approved=True,
            tags=tags,
            ingredients=ingredients,
            ingredient_disclosure_complete=payload.get("ingredient_disclosure_complete") is True,
            usage=cls._required_text(payload, "usage"),
            cautions=cls._required_text(payload, "cautions"),
        )

    def search_available(self, question: str, request_id: str) -> list[Product]:
        tags = self.scenario_tags(question)
        if not tags:
            return []
        response = self._post(
            "/internal/assistant/v1/catalog/candidates:search",
            request_id,
            {
                "request_id": request_id,
                "scenario_tags": tags,
                "category_codes": self._CATEGORY_CODES,
                "eligible_for_assistant": True,
                "limit": 20,
            },
        )
        items = response.get("items")
        if not isinstance(items, list) or len(items) > 20:
            raise MallCatalogError("mall_catalog_invalid_candidates")
        products = [self._product_from_payload(item, 1) for item in items if isinstance(item, dict)]
        if len(products) != len(items):
            raise MallCatalogError("mall_catalog_invalid_candidate_item")
        self._candidate_cache.update({item.sku_id: item for item in products})
        return products

    def get_realtime_available(self, sku_ids: list[str], request_id: str) -> list[Product]:
        unique_sku_ids = list(dict.fromkeys(sku_ids))
        if not unique_sku_ids or len(unique_sku_ids) > 20:
            raise MallCatalogError("mall_catalog_invalid_sku_batch")
        response = self._post(
            "/internal/assistant/v1/catalog/availability:batch",
            request_id,
            {
                "request_id": request_id,
                "purpose": "cart_draft_confirmation" if request_id.startswith("cart") else "recommendation",
                "consistency": "strong" if request_id.startswith("cart") else "bounded_staleness",
                "max_age_seconds": 5 if request_id.startswith("cart") else 30,
                "items": [{"sku_id": sku_id, "quantity": 1} for sku_id in unique_sku_ids],
            },
        )
        items = response.get("items")
        if not isinstance(items, list) or len(items) != len(unique_sku_ids):
            raise MallCatalogError("mall_catalog_incomplete_availability")
        max_age_seconds = 5 if request_id.startswith("cart") else 30
        available: list[Product] = []
        seen_sku_ids: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise MallCatalogError("mall_catalog_invalid_availability_item")
            sku_id = self._required_text(item, "sku_id")
            if sku_id not in unique_sku_ids or sku_id in seen_sku_ids:
                raise MallCatalogError("mall_catalog_invalid_availability_sku")
            seen_sku_ids.add(sku_id)
            quantity = item.get("available_quantity")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
                raise MallCatalogError("mall_catalog_invalid_available_quantity")
            if item.get("requested_quantity") != 1:
                raise MallCatalogError("mall_catalog_invalid_requested_quantity")
            checked_at_text = self._required_text(item, "inventory_checked_at")
            try:
                checked_at = datetime.fromisoformat(checked_at_text)
            except ValueError as error:
                raise MallCatalogError("mall_catalog_invalid_inventory_checked_at") from error
            if checked_at.tzinfo is None or (datetime.now(UTC) - checked_at).total_seconds() > max_age_seconds:
                raise MallCatalogError("mall_catalog_stale_availability")
            self._required_text(item, "price_version")
            self._required_text(item, "inventory_version")
            if item.get("is_sellable") is not True:
                continue
            price_fen = item.get("sale_price_fen")
            if isinstance(price_fen, bool) or not isinstance(price_fen, int) or price_fen < 0:
                raise MallCatalogError("mall_catalog_invalid_sale_price_fen")
            base = self._candidate_cache.get(sku_id)
            if base is None:
                # 服务重启后，确认令牌和套餐快照仍存在，但内存候选缓存不存在；上游必须返回展示快照。
                display = item.get("product")
                if not isinstance(display, dict):
                    raise MallCatalogError("mall_catalog_missing_candidate_snapshot")
                base = self._product_from_payload(
                    {
                        **display,
                        "sku_id": sku_id,
                        "sale_price_fen": price_fen,
                        "on_sale": item.get("on_sale"),
                        "assistant_approved": item.get("assistant_approved"),
                    },
                    quantity,
                )
            merged = base.model_copy(
                update={
                    "price_fen": price_fen,
                    "stock": quantity,
                    "on_sale": item.get("on_sale") is True,
                    "approved": item.get("assistant_approved") is True,
                }
            )
            if merged.price_fen < 0 or not merged.on_sale or not merged.approved or merged.stock < 1:
                continue
            available.append(merged)
        return available


def build_mall_catalog_gateway_from_env() -> MallCatalogGateway | None:
    """只有 CATALOG_SOURCE=mall 才接入真实源；配置不全时启动失败而非回退 Mock。"""
    source = os.getenv("CATALOG_SOURCE", "mock").lower()
    if source in {"mock", "database"}:
        return None
    if source != "mall":
        raise ValueError("CATALOG_SOURCE 只能是 mock、database 或 mall")
    required = {
        "MALL_CATALOG_BASE_URL": os.getenv("MALL_CATALOG_BASE_URL"),
        "MALL_OAUTH_TOKEN_URL": os.getenv("MALL_OAUTH_TOKEN_URL"),
        "MALL_OAUTH_CLIENT_ID": os.getenv("MALL_OAUTH_CLIENT_ID"),
        "MALL_OAUTH_CLIENT_SECRET": os.getenv("MALL_OAUTH_CLIENT_SECRET"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"商城读模型配置缺失：{', '.join(missing)}")
    try:
        timeout_seconds = int(os.getenv("MALL_CATALOG_TIMEOUT_SECONDS", "2"))
    except ValueError as error:
        raise ValueError("MALL_CATALOG_TIMEOUT_SECONDS 必须是整数") from error
    if not 1 <= timeout_seconds <= 5:
        raise ValueError("MALL_CATALOG_TIMEOUT_SECONDS 必须在 1 到 5 秒之间")
    client = httpx.Client()
    token_provider = OAuthClientCredentialsTokenProvider(
        token_url=str(required["MALL_OAUTH_TOKEN_URL"]),
        client_id=str(required["MALL_OAUTH_CLIENT_ID"]),
        client_secret=str(required["MALL_OAUTH_CLIENT_SECRET"]),
        scope=os.getenv("MALL_OAUTH_SCOPE", "catalog.assistant.read inventory.assistant.read"),
        client=client,
        timeout_seconds=timeout_seconds,
    )
    return MallCatalogGateway(
        base_url=str(required["MALL_CATALOG_BASE_URL"]),
        token_provider=token_provider,
        client=client,
        timeout_seconds=timeout_seconds,
    )
