from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import Catalog
from app.database_catalog import DatabaseCatalogRepository
from app.main import app
from app.oa.database import SessionLocal, engine
from app.oa.models import (
    Base,
    CatalogProduct,
    CatalogProductImage,
    Department,
    Permission,
    Role,
    User,
    new_id,
)
from app.oa.permission_catalog import PERMISSION_DEFINITIONS
from app.oa.security import hash_password
from app.service import SkinAssistantService

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"test-image"


async def reset_and_seed_catalog_admin() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        department = Department(id=new_id(), name="商品运营部", parent_id="")
        permissions = [
            Permission(code=code, name=name, kind=kind)
            for code, name, kind in PERMISSION_DEFINITIONS
            if code in {"catalog:product:read", "catalog:product:write"}
        ]
        role = Role(code="catalog_admin", name="商品管理员", data_scope="all", permissions=permissions)
        session.add_all(
            [
                department,
                *permissions,
                role,
                User(
                    username="catalog-admin",
                    display_name="商品管理员",
                    password_hash=hash_password("CorrectHorseBatteryStaple1!"),
                    department_id=department.id,
                    roles=[role],
                ),
            ]
        )
        await session.commit()


def login_catalog_admin(client: TestClient, username: str = "catalog-admin") -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "CorrectHorseBatteryStaple1!"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    return {"Authorization": f"Bearer {body['data']['access_token']}"}


def oa_data(response: object) -> dict[str, object]:
    """商品后台也必须经过 OA 的统一响应信封。"""
    body = response.json()  # type: ignore[union-attr]
    assert body["code"] == 0
    return body["data"]


def product_payload(image_id: str) -> dict[str, object]:
    return {
        "sku_id": "CJ-SERUM-30",
        "title": "澄肌屏障修护精华",
        "spec": "30ml",
        "price_fen": 26_800,
        "stock": 42,
        "description": "用于日常保湿修护。",
        "manual": "避光保存；开封后建议 12 个月内使用。",
        "ingredients": ["甘油", "神经酰胺"],
        "ingredient_disclosure_complete": True,
        "usage": "洁面后取适量均匀涂抹。",
        "cautions": "首次使用先局部测试。",
        "scenario_tags": ["屏障修护", "干燥感"],
        "on_sale": True,
        "assistant_approved": True,
        "image_ids": [image_id],
    }


async def reset_and_seed_catalog_self_scope() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        department = Department(id=new_id(), name="商品部", parent_id="")
        other_department = Department(id=new_id(), name="运营部", parent_id="")
        read = Permission(code="catalog:product:read", name="查看商品", kind="page")
        write = Permission(code="catalog:product:write", name="维护商品", kind="action")
        role = Role(
            code="catalog_self",
            name="本人商品运营",
            data_scope="self",
            permissions=[read, write],
        )
        actor = User(
            id=new_id(),
            username="scoped-user",
            display_name="本人运营",
            password_hash=hash_password("CorrectHorseBatteryStaple1!"),
            department_id=department.id,
            roles=[role],
        )
        colleague = User(
            id=new_id(),
            username="colleague",
            display_name="同部门运营",
            password_hash=hash_password("CorrectHorseBatteryStaple1!"),
            department_id=department.id,
        )
        outsider = User(
            id=new_id(),
            username="outsider",
            display_name="跨部门运营",
            password_hash=hash_password("CorrectHorseBatteryStaple1!"),
            department_id=other_department.id,
        )
        session.add_all(
            [
                department,
                other_department,
                read,
                write,
                role,
                actor,
                colleague,
                outsider,
                CatalogProduct(
                    id=new_id(), sku_id="SELF-001", title="本人商品", price_fen=100, stock=1,
                    ingredients=[], scenario_tags=[], created_by=actor.id,
                ),
                CatalogProduct(
                    id=new_id(), sku_id="TEAM-001", title="同部门商品", price_fen=100, stock=1,
                    ingredients=[], scenario_tags=[], created_by=colleague.id,
                ),
                CatalogProduct(
                    id=new_id(), sku_id="OTHER-001", title="跨部门商品", price_fen=100, stock=1,
                    ingredients=[], scenario_tags=[], created_by=outsider.id,
                ),
            ]
        )
        await session.commit()


def test_catalog_self_data_scope_hides_and_blocks_other_people_products() -> None:
    asyncio.run(reset_and_seed_catalog_self_scope())
    with TestClient(app) as client:
        headers = login_catalog_admin(client, "scoped-user")
        products = oa_data(client.get("/api/v1/catalog/products", headers=headers))["items"]
        assert [product["sku_id"] for product in products] == ["SELF-001"]

        async def find_product_id(sku_id: str) -> str:
            async with SessionLocal() as session:
                return await session.scalar(select(CatalogProduct.id).where(CatalogProduct.sku_id == sku_id))

        colleague_id = asyncio.run(find_product_id("TEAM-001"))
        assert client.get(f"/api/v1/catalog/products/{colleague_id}", headers=headers).status_code == 404
        assert (
            client.patch(
                f"/api/v1/catalog/products/{colleague_id}",
                json={"stock": 2, "expected_revision": 1},
                headers=headers,
            ).status_code
            == 404
        )


def test_catalog_upload_create_update_and_serve_image(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PRODUCT_UPLOAD_DIR", str(tmp_path / "uploads"))
    asyncio.run(reset_and_seed_catalog_admin())
    with TestClient(app) as client:
        assert client.get("/api/v1/catalog/products").status_code == 401
        headers = login_catalog_admin(client)

        mismatch = client.post(
            "/api/v1/catalog/uploads/images",
            files={"file": ("bad.jpg", PNG_HEADER, "image/jpeg")},
            headers=headers,
        )
        assert mismatch.status_code == 422

        upload = client.post(
            "/api/v1/catalog/uploads/images",
            files={"file": ("product.png", PNG_HEADER, "image/png")},
            headers=headers,
        )
        assert upload.status_code == 201
        image = oa_data(upload)

        created = client.post("/api/v1/catalog/products", json=product_payload(image["id"]), headers=headers)
        assert created.status_code == 201
        product = oa_data(created)
        assert product["image_urls"] == [image["url"]]
        assert product["revision"] == 1
        assert client.get(image["url"]).content == PNG_HEADER

        updated = client.patch(
            f"/api/v1/catalog/products/{product['id']}",
            json={"stock": 9, "expected_revision": 1},
            headers=headers,
        )
        assert updated.status_code == 200
        updated_data = oa_data(updated)
        assert updated_data["stock"] == 9
        assert updated_data["revision"] == 2
        conflict = client.patch(
            f"/api/v1/catalog/products/{product['id']}",
            json={"stock": 8, "expected_revision": 1},
            headers=headers,
        )
        assert conflict.status_code == 409


def test_database_catalog_source_filters_and_revalidates_stock(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("CATALOG_SOURCE", "database")
    repository = DatabaseCatalogRepository()
    Base.metadata.create_all(repository._engine)
    with Session(repository._engine) as session:
        session.add_all(
            [
                CatalogProduct(
                    id="product-serum",
                    sku_id="CJ-SERUM-30",
                    title="澄肌屏障修护精华",
                    spec="30ml",
                    price_fen=26_800,
                    stock=4,
                    ingredients=["甘油"],
                    ingredient_disclosure_complete=True,
                    usage="测试用法",
                    cautions="测试注意事项",
                    scenario_tags=["屏障修护", "干燥感"],
                    on_sale=True,
                    assistant_approved=True,
                ),
                CatalogProduct(
                    sku_id="CJ-HIDDEN-30",
                    title="未审核商品",
                    spec="30ml",
                    price_fen=9_900,
                    stock=8,
                    ingredients=[],
                    usage="测试用法",
                    cautions="测试注意事项",
                    scenario_tags=["屏障修护"],
                    on_sale=True,
                    assistant_approved=False,
                ),
            ]
        )
        session.add(
            CatalogProductImage(
                id="image-serum",
                product_id="product-serum",
                storage_key="serum-primary.png",
                content_type="image/png",
                size_bytes=len(PNG_HEADER),
                sort_order=0,
            )
        )
        session.commit()

    evidence = repository.evidence_for(["CJ-SERUM-30", "CJ-HIDDEN-30"])
    assert [item.sku_id for item in evidence] == ["CJ-SERUM-30"]
    assert evidence[0].version == "catalog-r1"
    assert "使用方法：测试用法" in evidence[0].quote

    catalog = Catalog(database_catalog=repository)
    candidates = catalog.search_available("换季干燥修护")
    assert [item.sku_id for item in candidates] == ["CJ-SERUM-30"]
    assert candidates[0].image_urls == ["/uploads/products/serum-primary.png"]
    # Agent SSE 的套餐项继续使用本次数据库实时快照，不能在这里回退到 mock SKU 或占位图片。
    agent_service = SkinAssistantService()
    agent_service.catalog = catalog
    agent_result = agent_service._bundle("换季干燥修护", [candidates[0].model_dump()])
    assert agent_result.bundle_id == "database_single_CJ-SERUM-30"
    assert agent_result.items[0].image_url == "/uploads/products/serum-primary.png"
    assert [item.sku_id for item in catalog.get_realtime_available(["CJ-SERUM-30"])] == [
        "CJ-SERUM-30"
    ]
    with Session(repository._engine) as session:
        product = session.query(CatalogProduct).filter_by(sku_id="CJ-SERUM-30").one()
        product.stock = 0
        session.commit()
    assert catalog.get_realtime_available(["CJ-SERUM-30"]) == []
