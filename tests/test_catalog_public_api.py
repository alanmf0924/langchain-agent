from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.customer.dependencies import get_current_customer
from app.main import app, service
from app.models import Product


def catalog_product() -> Product:
    return Product(
        sku_id="CJ-SERUM-30",
        name="澄肌屏障修护精华",
        spec="30ml",
        price_fen=26800,
        stock=8,
        on_sale=True,
        approved=True,
        tags=["屏障修护"],
        ingredients=["甘油", "神经酰胺"],
        ingredient_disclosure_complete=True,
        usage="洁面后取适量均匀涂抹。",
        cautions="首次使用先局部测试。",
        image_urls=["/uploads/products/serum.webp"],
        description="适合日常屏障修护。",
        manual="避光保存。",
    )


def test_public_catalog_only_returns_preview_fields(monkeypatch) -> None:
    monkeypatch.setattr(service, "available_products", lambda: [catalog_product()])
    with TestClient(app) as client:
        response = client.get("/api/catalog/products")

    assert response.status_code == 200
    assert response.json() == [
        {
            "sku_id": "CJ-SERUM-30",
            "name": "澄肌屏障修护精华",
            "spec": "30ml",
            "price_fen": 26800,
            "tags": ["屏障修护"],
            "image_urls": ["/uploads/products/serum.webp"],
        }
    ]


def test_product_detail_requires_customer_login() -> None:
    with TestClient(app) as client:
        response = client.get("/api/catalog/products/CJ-SERUM-30")

    assert response.status_code == 401


def test_customer_can_read_complete_on_sale_product(monkeypatch) -> None:
    app.dependency_overrides[get_current_customer] = lambda: SimpleNamespace(id="customer-1")
    monkeypatch.setattr(service, "visible_product", lambda _: catalog_product())
    try:
        with TestClient(app) as client:
            response = client.get("/api/catalog/products/CJ-SERUM-30")
    finally:
        app.dependency_overrides.pop(get_current_customer, None)

    assert response.status_code == 200
    assert response.json()["usage"] == "洁面后取适量均匀涂抹。"
    assert response.json()["ingredients"] == ["甘油", "神经酰胺"]


def test_product_detail_hides_product_that_is_no_longer_on_sale(monkeypatch) -> None:
    app.dependency_overrides[get_current_customer] = lambda: SimpleNamespace(id="customer-1")
    monkeypatch.setattr(service, "visible_product", lambda _: None)
    try:
        with TestClient(app) as client:
            response = client.get("/api/catalog/products/CJ-SERUM-30")
    finally:
        app.dependency_overrides.pop(get_current_customer, None)

    assert response.status_code == 404
