from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.customer.database import SessionLocal as CustomerSessionLocal
from app.customer.database import engine as customer_engine
from app.customer.models import Customer, CustomerBase
from app.customer.security import hash_password as hash_customer_password
from app.main import app
from app.oa.database import SessionLocal, engine
from app.oa.models import Base, CatalogProduct, Department, Permission, Role, User, new_id
from app.oa.security import hash_password

PASSWORD = "CorrectHorseBatteryStaple1!"


async def reset_and_seed_commerce() -> None:
    async with customer_engine.begin() as connection:
        await connection.run_sync(CustomerBase.metadata.drop_all)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with customer_engine.begin() as connection:
        await connection.run_sync(CustomerBase.metadata.create_all)

    async with SessionLocal() as session:
        department = Department(id=new_id(), name="商城运营部", parent_id="")
        customer_read = Permission(code="customer:profile:read", name="查看前台客户", kind="page")
        order_read = Permission(code="order:record:read", name="查看商城订单", kind="page")
        order_write = Permission(code="order:record:write", name="处理商城订单", kind="action")
        role = Role(
            code="commerce_operator",
            name="商城运营",
            data_scope="all",
            permissions=[customer_read, order_read, order_write],
        )
        session.add_all(
            [
                department,
                customer_read,
                order_read,
                order_write,
                role,
                User(
                    id=new_id(),
                    username="commerce-operator",
                    display_name="商城运营",
                    password_hash=hash_password(PASSWORD),
                    department_id=department.id,
                    roles=[role],
                ),
                CatalogProduct(
                    id="product-1",
                    sku_id="CJ-SERUM-30",
                    title="澄肌屏障修护精华",
                    spec="30ml",
                    price_fen=26_800,
                    stock=3,
                    on_sale=True,
                    ingredients=[],
                    scenario_tags=[],
                ),
            ]
        )
        await session.commit()

    async with CustomerSessionLocal() as session:
        session.add_all(
            [
                Customer(
                    id="customer-1",
                    username="shopper-one",
                    display_name="顾客一号",
                    email="one@example.test",
                    email_verified=True,
                    password_hash=hash_customer_password(PASSWORD),
                ),
                Customer(
                    id="customer-2",
                    username="shopper-two",
                    display_name="顾客二号",
                    email="two@example.test",
                    email_verified=True,
                    password_hash=hash_customer_password(PASSWORD),
                ),
            ]
        )
        await session.commit()


def customer_headers(client: TestClient, username: str = "shopper-one") -> dict[str, str]:
    response = client.post(
        "/api/customer/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def operator_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"username": "commerce-operator", "password": PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    return {"Authorization": f"Bearer {body['data']['access_token']}"}


def commerce_data(response):  # type: ignore[no-untyped-def]
    body = response.json()
    assert body["code"] == 0
    return body["data"]


def test_customer_order_is_owned_price_snapshotted_and_manually_fulfilled() -> None:
    asyncio.run(reset_and_seed_commerce())
    with TestClient(app) as client:
        shopper_headers = customer_headers(client)
        address = client.post(
            "/api/customer/addresses",
            json={
                "recipient_name": "顾客一号",
                "phone": "13800138000",
                "province": "上海市",
                "city": "上海市",
                "district": "徐汇区",
                "detail": "虹桥路 1 号",
                "is_default": True,
            },
            headers=shopper_headers,
        )
        assert address.status_code == 201
        address_id = address.json()["id"]

        created = client.post(
            "/api/customer/orders",
            json={
                "address_id": address_id,
                "items": [{"product_id": "product-1", "quantity": 2}],
                "customer_remark": "请工作日配送",
            },
            headers=shopper_headers,
        )
        assert created.status_code == 201
        order = created.json()
        assert order["status"] == "pending_payment"
        assert order["total_fen"] == 53_600
        assert order["items"] == [
            {
                "product_id": "product-1",
                "sku_id": "CJ-SERUM-30",
                "product_title": "澄肌屏障修护精华",
                "product_spec": "30ml",
                "unit_price_fen": 26_800,
                "quantity": 2,
                "subtotal_fen": 53_600,
            }
        ]

        other_headers = customer_headers(client, "shopper-two")
        assert (
            client.get(f"/api/customer/orders/{order['id']}", headers=other_headers).status_code
            == 404
        )

        operator = operator_headers(client)
        routes = commerce_data(client.get("/api/v1/routes", headers=operator))
        oa_directory = next(route for route in routes if route["path"] == "/oa")
        assert [item["path"] for item in oa_directory["children"]] == [
            "/oa/customers",
            "/oa/orders",
        ]
        customers = commerce_data(
            client.get("/api/v1/commerce/customers?keyword=shopper-one", headers=operator)
        )
        assert customers["total"] == 1
        assert customers["items"][0]["order_count"] == 1
        assert customers["items"][0]["order_total_fen"] == 53_600

        orders = commerce_data(
            client.get("/api/v1/commerce/orders?status=pending_payment", headers=operator)
        )
        assert [item["id"] for item in orders["items"]] == [order["id"]]

        paid = commerce_data(
            client.patch(
                f"/api/v1/commerce/orders/{order['id']}/status",
                json={"expected_status": "pending_payment", "status": "paid"},
                headers=operator,
            )
        )
        assert paid["status"] == "paid"
        conflict = client.patch(
            f"/api/v1/commerce/orders/{order['id']}/status",
            json={"expected_status": "pending_payment", "status": "cancelled"},
            headers=operator,
        )
        assert conflict.status_code == 409

        fulfilling = commerce_data(
            client.patch(
                f"/api/v1/commerce/orders/{order['id']}/status",
                json={
                    "expected_status": "paid",
                    "status": "fulfilling",
                    "fulfillment_note": "已交仓",
                },
                headers=operator,
            )
        )
        assert fulfilling["fulfillment_note"] == "已交仓"
        shipped = commerce_data(
            client.patch(
                f"/api/v1/commerce/orders/{order['id']}/status",
                json={"expected_status": "fulfilling", "status": "shipped", "tracking_no": "SF123"},
                headers=operator,
            )
        )
        assert shipped["status"] == "shipped"
        assert shipped["tracking_no"] == "SF123"

    async def product_stock() -> int:
        async with SessionLocal() as session:
            return await session.scalar(
                select(CatalogProduct.stock).where(CatalogProduct.id == "product-1")
            )

    assert asyncio.run(product_stock()) == 1


def test_order_api_requires_customer_ownership_and_oa_permissions() -> None:
    asyncio.run(reset_and_seed_commerce())
    with TestClient(app) as client:
        assert client.get("/api/customer/orders").status_code == 401
        assert client.get("/api/v1/commerce/orders").status_code == 401
        shopper_headers = customer_headers(client)
        assert client.get("/api/v1/commerce/orders", headers=shopper_headers).status_code == 401
