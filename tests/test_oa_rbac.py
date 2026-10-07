from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.customer.database import SessionLocal as CustomerSessionLocal
from app.customer.database import engine as customer_engine
from app.customer.models import Customer, CustomerBase
from app.customer.security import hash_password as hash_customer_password
from app.main import app
from app.oa.database import SessionLocal, engine
from app.oa.models import Base, Department, Permission, Role, User, department_roles, new_id
from app.oa.permission_catalog import PERMISSION_DEFINITIONS
from app.oa.security import hash_password


async def reset_and_seed() -> None:
    """分别准备客户与 OA 身份，验证二者不共享账户或访问令牌。"""
    async with customer_engine.begin() as connection:
        await connection.run_sync(CustomerBase.metadata.drop_all)
        await connection.run_sync(CustomerBase.metadata.create_all)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        department = Department(id=new_id(), name="技术部", parent_id="")
        permissions = [
            Permission(code=code, name=name, kind=kind)
            for code, name, kind in PERMISSION_DEFINITIONS
        ]
        admin_role = Role(
            code="system_admin",
            name="系统管理员",
            data_scope="all",
            permissions=permissions,
        )
        employee_role = Role(code="employee", name="普通员工", data_scope="self")
        for username, display_name, roles in [
            ("admin", "系统管理员", [admin_role]),
            ("employee", "普通员工", [employee_role]),
        ]:
            session.add(
                User(
                    username=username,
                    display_name=display_name,
                    password_hash=hash_password("CorrectHorseBatteryStaple1!"),
                    department_id=department.id,
                    roles=roles,
                )
            )
        session.add_all([department, *permissions, admin_role, employee_role])
        await session.flush()
        await session.execute(
            department_roles.insert(),
            [
                {"department_id": department.id, "role_id": admin_role.id},
                {"department_id": department.id, "role_id": employee_role.id},
            ],
        )
        await session.commit()
    async with CustomerSessionLocal() as session:
        session.add(
            Customer(
                username="customer",
                display_name="澄肌用户",
                password_hash=hash_customer_password("CorrectHorseBatteryStaple1!"),
            )
        )
        await session.commit()


def login(client: TestClient, username: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "CorrectHorseBatteryStaple1!"}
    )
    assert response.status_code == 200
    return oa_data(response)


def oa_data(response) -> object:  # type: ignore[no-untyped-def]
    """断言 OA 统一信封后返回业务 data，避免测试绕过真实接口契约。"""
    body = response.json()
    assert body["status"] == response.status_code
    assert body["message"]
    assert body["requestId"]
    assert body["code"] == 0
    return body["data"]


def customer_login(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/customer/auth/login",
        json={"username": "customer", "password": "CorrectHorseBatteryStaple1!"},
    )
    assert response.status_code == 200
    return response.json()


def test_oa_error_envelope_contains_status_message_and_request_id() -> None:
    asyncio.run(reset_and_seed())
    with TestClient(app) as client:
        denied = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "incorrect"}
        )
        assert denied.status_code == 401
        body = denied.json()
        assert body == {
            "code": 401,
            "status": 401,
            "message": "账号或密码不正确",
            "data": None,
            "requestId": body["requestId"],
        }
        assert denied.headers["x-request-id"] == body["requestId"]

        invalid = client.post("/api/v1/auth/login", json={"username": "中文", "password": "x"})
        assert invalid.status_code == 422
        invalid_body = invalid.json()
        assert invalid_body["code"] == 422
        assert invalid_body["status"] == 422
        assert invalid_body["message"] == "请求参数校验失败"
        assert invalid_body["data"] is None
        assert invalid_body["requestId"]
        assert invalid_body["errors"]


def test_oa_login_and_button_permission_are_enforced_server_side() -> None:
    asyncio.run(reset_and_seed())
    with TestClient(app) as client:
        admin = login(client, "admin")
        headers = {"Authorization": f"Bearer {admin['access_token']}"}
        assert oa_data(client.get("/api/v1/auth/me", headers=headers))["role_codes"] == [
            "system_admin"
        ]
        assert client.get("/api/v1/system/users", headers=headers).status_code == 200
        routes = client.get("/api/v1/routes", headers=headers)
        assert routes.status_code == 200
        route_data = oa_data(routes)
        oa_route = next(route for route in route_data if route["path"] == "/oa")
        assert "component" not in oa_route
        assert "name" not in oa_route
        user_route = next(child for child in oa_route["children"] if child["path"] == "/oa/system/users")
        assert user_route["component"] == "oa/users/index"
        assert "*:*:*" not in oa_data(client.get("/api/v1/auth/me", headers=headers))["permission_codes"]
        # Pure Admin 将 children: [] 视为待过滤的空目录；叶子页面必须省略该字段。
        assert "children" not in user_route

        employee = login(client, "employee")
        denied = client.get(
            "/api/v1/system/users", headers={"Authorization": f"Bearer {employee['access_token']}"}
        )
        assert denied.status_code == 403
        assert (
            oa_data(
                client.get(
                    "/api/v1/routes",
                    headers={"Authorization": f"Bearer {employee['access_token']}"},
                )
            )
            == []
        )


def test_oa_rejects_wildcard_permission_resource() -> None:
    asyncio.run(reset_and_seed())
    with TestClient(app) as client:
        admin = login(client, "admin")
        response = client.post(
            "/api/v1/system/permissions",
            json={"code": "*:*:*", "name": "通配权限", "kind": "action"},
            headers={"Authorization": f"Bearer {admin['access_token']}"},
        )
        assert response.status_code == 422
        assert response.json()["data"] is None


def test_assistant_endpoints_require_a_logged_in_session() -> None:
    asyncio.run(reset_and_seed())
    with TestClient(app) as client:
        denied = client.post("/api/agent/runs", json={"question": "换季干燥，预算 600 元"})
        assert denied.status_code == 401

        employee = login(client, "employee")
        employee_headers = {"Authorization": f"Bearer {employee['access_token']}"}
        assert (
            client.post(
                "/api/cart-drafts",
                json={"confirmation_token": "invalid", "confirmed": True},
                headers=employee_headers,
            ).status_code
            == 401
        )

        account = customer_login(client)
        headers = {"Authorization": f"Bearer {account['access_token']}"}
        # 无效令牌应到达请求校验（422），而不是被错误地当作未认证请求。
        protected = client.post(
            "/api/cart-drafts",
            json={"confirmation_token": "invalid", "confirmed": True},
            headers=headers,
        )
        assert protected.status_code == 422


def test_operations_endpoints_require_an_oa_permission() -> None:
    asyncio.run(reset_and_seed())
    with TestClient(app) as client:
        assert client.get("/api/knowledge/status").status_code == 401

        customer = customer_login(client)
        assert (
            client.get(
                "/api/knowledge/status",
                headers={"Authorization": f"Bearer {customer['access_token']}"},
            ).status_code
            == 401
        )

        admin = login(client, "admin")
        response = client.get(
            "/api/knowledge/status", headers={"Authorization": f"Bearer {admin['access_token']}"}
        )
        assert response.status_code == 200


def test_oa_refresh_requires_csrf_and_rotates_session() -> None:
    asyncio.run(reset_and_seed())
    with TestClient(app) as client:
        login(client, "admin")
        assert client.post("/api/v1/auth/refresh").status_code == 401

    asyncio.run(reset_and_seed())
    with TestClient(app) as client:
        login(client, "admin")
        csrf_token = client.cookies.get("oa_csrf_token")
        refreshed = client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": csrf_token})
        assert refreshed.status_code == 200
        assert oa_data(refreshed)["access_token"]


def test_oa_management_resources_are_isolated_and_server_authorized() -> None:
    asyncio.run(reset_and_seed())
    with TestClient(app) as client:
        admin = login(client, "admin")
        headers = {"Authorization": f"Bearer {admin['access_token']}"}

        # OA 身份表使用 oa_ 前缀，不会占用商城前台未来的 users 表。
        assert "oa_users" in Base.metadata.tables
        assert "users" not in Base.metadata.tables

        department = client.post(
            "/api/v1/system/departments", json={"name": "运营部"}, headers=headers
        )
        assert department.status_code == 201
        permission = client.post(
            "/api/v1/system/permissions",
            json={"code": "catalog:read", "name": "查看商品", "kind": "action"},
            headers=headers,
        )
        assert permission.status_code == 201
        role = client.post(
            "/api/v1/system/roles",
            json={
                "code": "catalog_operator",
                "name": "商品运营",
                "data_scope": "department",
                "permission_codes": ["catalog:read"],
            },
            headers=headers,
        )
        assert role.status_code == 201
        paged_departments = oa_data(
            client.get("/api/v1/system/departments?page=1&page_size=1", headers=headers)
        )
        assert paged_departments["page"] == 1
        assert paged_departments["page_size"] == 1
        assert paged_departments["total"] >= 2
        assert len(paged_departments["items"]) == 1
        configured_department = client.post(
            "/api/v1/system/departments",
            json={"name": "新媒体部", "role_codes": ["catalog_operator"]},
            headers=headers,
        )
        assert configured_department.status_code == 201
        assert oa_data(configured_department)["role_codes"] == ["catalog_operator"]
        assert (
            client.put(
                f"/api/v1/authorization/departments/{oa_data(department)['id']}/roles",
                json={"role_codes": ["catalog_operator"]},
                headers=headers,
            ).status_code
            == 200
        )
        user = client.post(
            "/api/v1/system/users",
            json={
                "username": "operator",
                "password": "CorrectHorseBatteryStaple1!",
                "display_name": "商品运营员",
                "department_id": oa_data(department)["id"],
                "role_codes": ["catalog_operator"],
            },
            headers=headers,
        )
        assert user.status_code == 201
        assert oa_data(user)["department_id"] == oa_data(department)["id"]
        assert oa_data(user)["role_codes"] == ["catalog_operator"]

        # 停用会使已有账号立即无法登录；角色仍被引用时不能被误删。
        disabled = client.patch(
            f"/api/v1/system/users/{oa_data(user)['id']}", json={"is_active": False}, headers=headers
        )
        assert disabled.status_code == 200
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": "operator", "password": "CorrectHorseBatteryStaple1!"},
            ).status_code
            == 401
        )
        assert (
            client.delete(f"/api/v1/system/roles/{oa_data(role)['id']}", headers=headers).status_code
            == 409
        )
        assert (
            client.delete(f"/api/v1/system/users/{oa_data(user)['id']}", headers=headers).status_code
            == 200
        )
        assert (
            oa_data(client.get(f"/api/v1/system/users/{oa_data(user)['id']}", headers=headers))[
                "is_active"
            ]
            is False
        )


def test_personnel_records_accept_chinese_names_and_remain_non_login_data() -> None:
    asyncio.run(reset_and_seed())
    with TestClient(app) as client:
        admin = login(client, "admin")
        headers = {"Authorization": f"Bearer {admin['access_token']}"}
        department = client.post(
            "/api/v1/system/departments", json={"name": "美妆运营部"}, headers=headers
        )
        assert department.status_code == 201

        created = client.post(
            "/api/v1/organization/personnel",
            json={
                "name": "王小美",
                "phone": "138 0013 8000",
                "email": "xiaomei@example.com",
                "gender": "female",
                "department_id": oa_data(department)["id"],
            },
            headers=headers,
        )
        assert created.status_code == 201
        assert oa_data(created)["name"] == "王小美"
        assert oa_data(created)["department_id"] == oa_data(department)["id"]
        # 档案姓名不是 username；中文姓名会先被登录账号格式校验拒绝。
        assert client.post(
            "/api/v1/auth/login", json={"username": "王小美", "password": "not-an-account"}
        ).status_code == 422

        listed = client.get("/api/v1/organization/personnel", headers=headers)
        assert listed.status_code == 200
        assert [item["name"] for item in oa_data(listed)["items"]] == ["王小美"]
        assert (
            client.delete(
                f"/api/v1/system/departments/{oa_data(department)['id']}", headers=headers
            ).status_code
            == 409
        )

        assert (
            client.delete(
                f"/api/v1/organization/personnel/{oa_data(created)['id']}", headers=headers
            ).status_code
            == 200
        )
        assert oa_data(client.get("/api/v1/organization/personnel", headers=headers))["items"] == []
        assert oa_data(
            client.get(
                "/api/v1/organization/personnel?include_inactive=true", headers=headers
            )
        )["total"] == 1

        employee = login(client, "employee")
        denied = client.get(
            "/api/v1/organization/personnel",
            headers={"Authorization": f"Bearer {employee['access_token']}"},
        )
        assert denied.status_code == 403


def test_department_role_constraints_and_personnel_exception_grants_are_traceable() -> None:
    asyncio.run(reset_and_seed())
    with TestClient(app) as client:
        admin = login(client, "admin")
        headers = {"Authorization": f"Bearer {admin['access_token']}"}
        department = client.post(
            "/api/v1/system/departments", json={"name": "品牌部"}, headers=headers
        )
        assert department.status_code == 201
        for code, name in [("catalog:read", "查看商品"), ("catalog:write", "编辑商品")]:
            assert (
                client.post(
                    "/api/v1/system/permissions",
                    json={"code": code, "name": name, "kind": "action"},
                    headers=headers,
                ).status_code
                == 201
            )
        assert (
            client.post(
                "/api/v1/system/roles",
                json={
                    "code": "brand_operator",
                    "name": "品牌运营",
                    "data_scope": "department",
                    "permission_codes": ["catalog:read"],
                },
                headers=headers,
            ).status_code
            == 201
        )
        # 角色尚未启用于部门时，账号创建必须被后端拒绝。
        blocked_user = client.post(
            "/api/v1/system/users",
            json={
                "username": "brand_operator",
                "password": "CorrectHorseBatteryStaple1!",
                "display_name": "品牌运营员",
                "department_id": oa_data(department)["id"],
                "role_codes": ["brand_operator"],
            },
            headers=headers,
        )
        assert blocked_user.status_code == 422
        department_roles_response = client.put(
            f"/api/v1/authorization/departments/{oa_data(department)['id']}/roles",
            json={"role_codes": ["brand_operator"]},
            headers=headers,
        )
        assert department_roles_response.status_code == 200
        assert oa_data(department_roles_response)["role_codes"] == ["brand_operator"]

        user = client.post(
            "/api/v1/system/users",
            json={
                "username": "brand_operator",
                "password": "CorrectHorseBatteryStaple1!",
                "display_name": "品牌运营员",
                "department_id": oa_data(department)["id"],
                "role_codes": ["brand_operator"],
            },
            headers=headers,
        )
        assert user.status_code == 201
        personnel = client.post(
            "/api/v1/organization/personnel",
            json={"name": "李美妆", "department_id": oa_data(department)["id"]},
            headers=headers,
        )
        assert personnel.status_code == 201
        linked = client.put(
            f"/api/v1/authorization/personnel/{oa_data(personnel)['id']}/account",
            json={"user_id": oa_data(user)["id"]},
            headers=headers,
        )
        assert linked.status_code == 200
        granted = client.put(
            f"/api/v1/authorization/personnel/{oa_data(personnel)['id']}/permission-grants",
            json={"permission_codes": ["catalog:write"]},
            headers=headers,
        )
        assert granted.status_code == 200
        assert oa_data(granted)["direct_permission_codes"] == ["catalog:write"]
        effective = client.get(
            f"/api/v1/authorization/personnel/{oa_data(personnel)['id']}/effective-access",
            headers=headers,
        )
        assert effective.status_code == 200
        assert oa_data(effective)["department_role_codes"] == ["brand_operator"]
        assert {item["code"] for item in oa_data(effective)["effective_permissions"]} == {
            "catalog:read",
            "catalog:write",
        }
        assert any(
            item["source"] == "direct" and item["code"] == "catalog:write"
            for item in oa_data(effective)["effective_permissions"]
        )
