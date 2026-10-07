import json
from datetime import UTC, datetime

import httpx
import pytest

from app.catalog import Catalog
from app.mall_catalog import (
    MallCatalogError,
    MallCatalogGateway,
    build_mall_catalog_gateway_from_env,
)


class StaticTokenProvider:
    def get_access_token(self) -> str:
        return "test-access-token"


def _candidate(sku_id: str) -> dict[str, object]:
    return {
        "sku_id": sku_id,
        "spu_id": "TST-SPU-SERUM",
        "name": "测试修护精华",
        "spec": "30ml",
        "category_code": "FACE_SERUM",
        "scenario_tags": ["屏障修护", "干燥感"],
        "assistant_approved": True,
        "on_sale": True,
        "sale_price_fen": 26_990,
        "price_version": "price_test_1",
        "product_updated_at": datetime.now(UTC).isoformat(),
        "ingredients": ["测试成分"],
        "usage": "测试用法。",
        "cautions": "测试注意事项。",
    }


def test_mall_gateway_sends_only_tags_and_requires_fresh_complete_availability() -> None:
    sku_id = "TST-SKIN-SERUM-30"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-access-token"
        assert request.headers["x-request-id"] == "run_mall_contract"
        payload = json.loads(request.content)
        assert "question" not in payload
        if request.url.path.endswith("candidates:search"):
            assert payload["scenario_tags"]
            return httpx.Response(200, json={"items": [_candidate(sku_id)]})
        assert payload["purpose"] == "recommendation"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "sku_id": sku_id,
                        "requested_quantity": 1,
                        "is_sellable": True,
                        "available_quantity": 8,
                        "on_sale": True,
                        "assistant_approved": True,
                        "sale_price_fen": 26_990,
                        "price_version": "price_test_1",
                        "inventory_version": "inventory_test_9",
                        "inventory_checked_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
        )

    gateway = MallCatalogGateway(
        base_url="https://mall.test",
        token_provider=StaticTokenProvider(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        timeout_seconds=2,
    )
    catalog = Catalog(mall_gateway=gateway)

    candidates = catalog.search_available("换季干燥紧绷", "run_mall_contract")
    available = catalog.get_realtime_available([sku_id], "run_mall_contract")

    assert candidates[0].price_fen == 26_990
    assert available[0].stock == 8
    assert available[0].price_fen == 26_990


def test_mall_gateway_rejects_stale_availability() -> None:
    sku_id = "TST-SKIN-SERUM-30"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("candidates:search"):
            return httpx.Response(200, json={"items": [_candidate(sku_id)]})
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "sku_id": sku_id,
                        "requested_quantity": 1,
                        "is_sellable": True,
                        "available_quantity": 8,
                        "on_sale": True,
                        "assistant_approved": True,
                        "sale_price_fen": 26_990,
                        "price_version": "price_test_1",
                        "inventory_version": "inventory_test_9",
                        "inventory_checked_at": "2020-01-01T00:00:00+00:00",
                    }
                ]
            },
        )

    gateway = MallCatalogGateway(
        base_url="https://mall.test",
        token_provider=StaticTokenProvider(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        timeout_seconds=2,
    )
    gateway.search_available("换季干燥紧绷", "run_mall_stale")

    with pytest.raises(MallCatalogError, match="stale"):
        gateway.get_realtime_available([sku_id], "run_mall_stale")


def test_mall_gateway_can_revalidate_after_restart_from_availability_snapshot() -> None:
    sku_id = "TST-SKIN-SERUM-30"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "sku_id": sku_id,
                        "requested_quantity": 1,
                        "is_sellable": True,
                        "available_quantity": 8,
                        "on_sale": True,
                        "assistant_approved": True,
                        "sale_price_fen": 26_990,
                        "price_version": "price_test_1",
                        "inventory_version": "inventory_test_9",
                        "inventory_checked_at": datetime.now(UTC).isoformat(),
                        "product": _candidate(sku_id),
                    }
                ]
            },
        )

    restarted_gateway = MallCatalogGateway(
        base_url="https://mall.test",
        token_provider=StaticTokenProvider(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        timeout_seconds=2,
    )

    available = restarted_gateway.get_realtime_available([sku_id], "cart_persisted_confirmation")
    assert available[0].name == "测试修护精华"
    assert available[0].price_fen == 26_990


def test_mall_source_configuration_never_falls_back_to_mock(monkeypatch) -> None:
    monkeypatch.setenv("CATALOG_SOURCE", "mall")
    monkeypatch.delenv("MALL_CATALOG_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="MALL_CATALOG_BASE_URL"):
        build_mall_catalog_gateway_from_env()
