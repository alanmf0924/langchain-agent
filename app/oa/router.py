from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.oa.api_contract import OaApiRoute
from app.oa.config import get_oa_settings
from app.oa.database import get_session
from app.oa.dependencies import get_current_user, require_permission
from app.oa.models import (
    Department,
    Permission,
    Personnel,
    RefreshSession,
    Role,
    User,
    department_roles,
    new_id,
    role_permissions,
    user_permission_grants,
    user_roles,
)
from app.oa.schemas import (
    AuthorizationDepartmentItem,
    AuthorizationPersonnelItem,
    CurrentUserResponse,
    DepartmentCreate,
    DepartmentItem,
    DepartmentRoleUpdate,
    DepartmentUpdate,
    EffectiveAccessItem,
    EffectivePermissionSource,
    LoginRequest,
    LoginResponse,
    OaUserCreate,
    OaUserItem,
    OaUserPasswordReset,
    OaUserUpdate,
    PageResult,
    PermissionCreate,
    PermissionItem,
    PermissionUpdate,
    PersonnelAccountLinkUpdate,
    PersonnelCreate,
    PersonnelItem,
    PersonnelPermissionGrantUpdate,
    PersonnelUpdate,
    RoleCreate,
    RoleItem,
    RoleUpdate,
    RouteItem,
    RouteMeta,
)
from app.oa.security import hash_opaque_token, hash_password, verify_password
from app.oa.services import (
    current_user_response,
    issue_session,
    load_user,
    permission_codes,
    revoke_user_sessions,
    write_audit_log,
)

router = APIRouter(
    prefix="/api/v1", tags=["OA 鉴权与权限"], route_class=OaApiRoute
)


def department_item(department: Department) -> DepartmentItem:
    return DepartmentItem(
        id=department.id,
        name=department.name,
        parent_id=department.parent_id,
        is_active=department.is_active,
        role_codes=sorted(role.code for role in department.roles),
    )


def personnel_item(personnel: Personnel) -> PersonnelItem:
    return PersonnelItem(
        id=personnel.id,
        name=personnel.name,
        phone=personnel.phone,
        email=personnel.email,
        gender=personnel.gender,
        department_id=personnel.department_id,
        is_active=personnel.is_active,
    )


def permission_item(permission: Permission) -> PermissionItem:
    return PermissionItem(
        id=permission.id, code=permission.code, name=permission.name, kind=permission.kind
    )


def role_item(role: Role) -> RoleItem:
    return RoleItem(
        id=role.id,
        code=role.code,
        name=role.name,
        data_scope=role.data_scope,
        is_active=role.is_active,
        permission_codes=sorted(permission.code for permission in role.permissions),
    )


def oa_user_item(user: User) -> OaUserItem:
    return OaUserItem(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        department_id=user.department_id,
        is_active=user.is_active,
        role_codes=sorted(role.code for role in user.roles),
    )


def authorization_personnel_item(
    personnel: Personnel, user: User | None
) -> AuthorizationPersonnelItem:
    return AuthorizationPersonnelItem(
        id=personnel.id,
        name=personnel.name,
        department_id=personnel.department_id,
        is_active=personnel.is_active,
        user_id=user.id if user else None,
        username=user.username if user else "",
        display_name=user.display_name if user else "",
        assigned_role_codes=sorted(role.code for role in user.roles) if user else [],
        direct_permission_codes=sorted(permission.code for permission in user.permission_grants)
        if user
        else [],
    )


async def department_or_404(session: AsyncSession, department_id: str) -> Department:
    department = await session.scalar(
        select(Department).options(selectinload(Department.roles)).where(Department.id == department_id)
    )
    if department is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="department not found")
    return department


async def personnel_or_404(session: AsyncSession, personnel_id: str) -> Personnel:
    personnel = await session.get(Personnel, personnel_id)
    if personnel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="personnel not found")
    return personnel


async def user_or_404(session: AsyncSession, user_id: str) -> User:
    user = await load_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OA user not found")
    return user


async def role_or_404(session: AsyncSession, role_id: str) -> Role:
    role = await session.scalar(
        select(Role).options(selectinload(Role.permissions)).where(Role.id == role_id)
    )
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="role not found")
    return role


async def permission_or_404(session: AsyncSession, permission_id: str) -> Permission:
    permission = await session.get(Permission, permission_id)
    if permission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="permission not found")
    return permission


async def resolve_permissions(session: AsyncSession, codes: list[str]) -> list[Permission]:
    unique_codes = list(dict.fromkeys(codes))
    if not unique_codes:
        return []
    permissions = (
        await session.scalars(select(Permission).where(Permission.code.in_(unique_codes)))
    ).all()
    by_code = {permission.code: permission for permission in permissions}
    missing = sorted(set(unique_codes) - set(by_code))
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown permission codes: {', '.join(missing)}",
        )
    return [by_code[code] for code in unique_codes]


async def resolve_roles(session: AsyncSession, codes: list[str]) -> list[Role]:
    unique_codes = list(dict.fromkeys(codes))
    if not unique_codes:
        return []
    roles = (
        await session.scalars(
            select(Role).options(selectinload(Role.permissions)).where(Role.code.in_(unique_codes))
        )
    ).all()
    by_code = {role.code: role for role in roles}
    missing = sorted(set(unique_codes) - set(by_code))
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown role codes: {', '.join(missing)}",
        )
    inactive = sorted(role.code for role in roles if not role.is_active)
    if inactive:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"inactive role codes cannot be assigned: {', '.join(inactive)}",
        )
    return [by_code[code] for code in unique_codes]


async def department_role_codes(session: AsyncSession, department_id: str) -> list[str]:
    codes = (
        await session.scalars(
            select(Role.code)
            .join(department_roles, department_roles.c.role_id == Role.id)
            .where(department_roles.c.department_id == department_id, Role.is_active.is_(True))
            .order_by(Role.code)
        )
    ).all()
    return list(codes)


async def assert_roles_enabled_for_department(
    session: AsyncSession, department_id: str, roles: list[Role]
) -> None:
    available = set(await department_role_codes(session, department_id))
    missing = sorted(role.code for role in roles if role.code not in available)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"roles are not enabled for department: {', '.join(missing)}",
        )


async def replace_department_roles(
    session: AsyncSession, department: Department, role_codes: list[str]
) -> list[Role]:
    """替换部门可分配角色，且不允许让已有账号失去当前角色。"""
    roles = await resolve_roles(session, role_codes)
    desired_role_codes = {role.code for role in roles}
    assigned_role_codes = set(
        (
            await session.scalars(
                select(Role.code)
                .join(user_roles, user_roles.c.role_id == Role.id)
                .join(User, User.id == user_roles.c.user_id)
                .where(User.department_id == department.id)
            )
        ).all()
    )
    missing_assigned_roles = sorted(assigned_role_codes - desired_role_codes)
    if missing_assigned_roles:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "cannot disable roles assigned to current department users: "
                f"{', '.join(missing_assigned_roles)}"
            ),
        )
    department.roles = roles
    return roles


def page_result(items: list[object], page: int, page_size: int, total: int) -> PageResult:
    return PageResult(items=items, page=page, page_size=page_size, total=total)


def set_refresh_cookies(response: Response, refresh_token: str, csrf_token: str) -> None:
    settings = get_oa_settings()
    options = {
        "max_age": settings.refresh_token_ttl_seconds,
        "secure": settings.cookie_secure,
        "samesite": "lax",
    }
    response.set_cookie(
        "oa_refresh_token",
        refresh_token,
        httponly=True,
        path="/api/v1/auth",
        **options,
    )
    # SPA 页面位于根路径，CSRF Cookie 必须能被 JavaScript 读取并回传到认证接口。
    # refresh token 仍限制在认证路径且保持 HttpOnly，不能被前端脚本读取。
    response.delete_cookie("oa_csrf_token", path="/api/v1/auth")
    response.set_cookie("oa_csrf_token", csrf_token, httponly=False, path="/", **options)


def clear_refresh_cookies(response: Response) -> None:
    response.delete_cookie("oa_refresh_token", path="/api/v1/auth")
    response.delete_cookie("oa_csrf_token", path="/api/v1/auth")
    response.delete_cookie("oa_csrf_token", path="/")


def is_expired(value: datetime, now: datetime) -> bool:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)) < now


@router.post("/auth/login", response_model=LoginResponse, summary="OA 用户登录")
async def login(
    payload: LoginRequest, response: Response, session: AsyncSession = Depends(get_session)
) -> LoginResponse:
    user = await session.scalar(select(User).where(User.username == payload.username))
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        await write_audit_log(
            session, actor_id=None, action="auth.login", outcome="denied", detail=payload.username
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码不正确"
        )
    user = await load_user(session, user.id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码不正确"
        )
    access_token, refresh_token, csrf_token = await issue_session(session, user)
    await write_audit_log(session, actor_id=user.id, action="auth.login", outcome="success")
    await session.commit()
    set_refresh_cookies(response, refresh_token, csrf_token)
    return LoginResponse(
        access_token=access_token,
        expires_in=get_oa_settings().access_token_ttl_seconds,
        user=current_user_response(user),
    )


@router.post("/auth/refresh", response_model=LoginResponse, summary="轮换刷新令牌")
async def refresh(
    request: Request,
    response: Response,
    oa_refresh_token: str = Cookie(default=""),
    oa_csrf_token: str = Cookie(default=""),
    session: AsyncSession = Depends(get_session),
) -> LoginResponse:
    csrf_header, now = request.headers.get("x-csrf-token", ""), datetime.now(UTC)
    record = await session.scalar(
        select(RefreshSession).where(
            RefreshSession.token_hash == hash_opaque_token(oa_refresh_token)
        )
    )
    if (
        not oa_refresh_token
        or not csrf_header
        or csrf_header != oa_csrf_token
        or record is None
        or record.revoked_at is not None
        or is_expired(record.expires_at, now)
        or record.csrf_hash != hash_opaque_token(csrf_header)
    ):
        clear_refresh_cookies(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录会话已失效，请重新登录"
        )
    user = await load_user(session, record.user_id)
    if user is None or not user.is_active:
        record.revoked_at = now
        await session.commit()
        clear_refresh_cookies(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录会话已失效，请重新登录"
        )
    record.revoked_at = now
    access_token, refresh_token, csrf_token = await issue_session(session, user)
    await session.commit()
    set_refresh_cookies(response, refresh_token, csrf_token)
    return LoginResponse(
        access_token=access_token,
        expires_in=get_oa_settings().access_token_ttl_seconds,
        user=current_user_response(user),
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, summary="退出登录")
async def logout(
    request: Request,
    response: Response,
    oa_refresh_token: str = Cookie(default=""),
    oa_csrf_token: str = Cookie(default=""),
    session: AsyncSession = Depends(get_session),
) -> None:
    csrf_header = request.headers.get("x-csrf-token", "")
    if oa_refresh_token and csrf_header and csrf_header == oa_csrf_token:
        record = await session.scalar(
            select(RefreshSession).where(
                RefreshSession.token_hash == hash_opaque_token(oa_refresh_token)
            )
        )
        if record is not None and record.csrf_hash == hash_opaque_token(csrf_header):
            record.revoked_at = datetime.now(UTC)
            await session.commit()
    clear_refresh_cookies(response)


@router.get("/auth/me", response_model=CurrentUserResponse, summary="获取当前用户与权限")
async def me(user: User = Depends(get_current_user)) -> CurrentUserResponse:
    return current_user_response(user)


@router.get(
    "/routes",
    response_model=list[RouteItem],
    response_model_exclude_none=True,
    summary="获取当前用户的后台路由",
)
async def routes(user: User = Depends(get_current_user)) -> list[RouteItem]:
    """按明确的页面权限返回与 Pure Admin mock 一致的目录与叶子页面树。"""
    codes = permission_codes(user)

    def page(
        *,
        permission: str,
        path: str,
        component: str,
        name: str,
        title: str,
        icon: str,
        rank: int,
        action_permissions: tuple[str, ...] = (),
    ) -> RouteItem | None:
        if permission not in codes:
            return None
        return RouteItem(
            path=path,
            component=component,
            name=name,
            meta=RouteMeta(
                title=title,
                icon=icon,
                rank=rank,
                # 页面可见性由 permission 判定；角色名只用于归属展示，不能再充当路由授权。
                roles=[],
                auths=[code for code in action_permissions if code in codes],
            ),
        )

    def directory(
        *, path: str, title: str, icon: str, rank: int, children: list[RouteItem | None]
    ) -> RouteItem | None:
        visible_children = [child for child in children if child is not None]
        if not visible_children:
            return None
        return RouteItem(
            path=path,
            meta=RouteMeta(title=title, icon=icon, rank=rank, roles=[]),
            children=visible_children,
        )

    routes = [
        directory(
            path="/organization",
            title="部门管理",
            icon="ep:office-building",
            rank=27,
            children=[
                page(
                    permission="system:personnel:read",
                    path="/organization/personnel",
                    component="personnel/index",
                    name="OrganizationPersonnel",
                    title="人员档案",
                    icon="ep:user-filled",
                    rank=10,
                    action_permissions=("system:personnel:write",),
                ),
                page(
                    permission="system:department:read",
                    path="/organization/departments",
                    component="oa/departments/index",
                    name="OrganizationDepartments",
                    title="部门设置",
                    icon="ep:office-building",
                    rank=20,
                    action_permissions=("system:department:write",),
                ),
            ],
        ),
        directory(
            path="/oa",
            title="美妆选购OA",
            icon="ep:setting",
            rank=28,
            children=[
                page(
                    permission="system:user:read",
                    path="/oa/system/users",
                    component="oa/users/index",
                    name="OaSystemUsers",
                    title="用户管理",
                    icon="ep:user",
                    rank=10,
                    action_permissions=("system:user:write",),
                ),
                page(
                    permission="catalog:product:read",
                    path="/oa/catalog",
                    component="oa/catalog/index",
                    name="OaCatalog",
                    title="商品管理",
                    icon="ep:goods",
                    rank=20,
                    action_permissions=("catalog:product:write",),
                ),
                page(
                    permission="customer:profile:read",
                    path="/oa/customers",
                    component="oa/customers/index",
                    name="OaCustomers",
                    title="前台客户",
                    icon="ep:user-filled",
                    rank=30,
                ),
                page(
                    permission="order:record:read",
                    path="/oa/orders",
                    component="oa/orders/index",
                    name="OaOrders",
                    title="订单管理",
                    icon="ep:document-checked",
                    rank=40,
                    action_permissions=("order:record:write",),
                ),
            ],
        ),
        directory(
            path="/permissions",
            title="权限管理",
            icon="ep:key",
            rank=29,
            children=[
                page(
                    permission="system:authorization:read",
                    path="/permissions/organization-authorization",
                    component="permissions/authorization/index",
                    name="PermissionOrganizationAuthorization",
                    title="组织授权",
                    icon="ep:connection",
                    rank=10,
                    action_permissions=("system:authorization:write",),
                ),
                page(
                    permission="system:role:read",
                    path="/permissions/roles",
                    component="oa/roles/index",
                    name="PermissionRoles",
                    title="角色管理",
                    icon="ep:user-filled",
                    rank=20,
                    action_permissions=("system:role:write",),
                ),
                page(
                    permission="system:permission:read",
                    path="/permissions/resources",
                    component="oa/permissions/index",
                    name="PermissionResources",
                    title="权限资源",
                    icon="ep:menu",
                    rank=30,
                    action_permissions=("system:permission:write",),
                ),
                page(
                    permission="system:authorization:read",
                    path="/permissions/effective-access",
                    component="oa/access/index",
                    name="PermissionEffectiveAccess",
                    title="当前权限",
                    icon="ep:checked",
                    rank=40,
                ),
                page(
                    permission="system:role:read",
                    path="/permissions/overview",
                    component="oa/rbac/index",
                    name="PermissionOverview",
                    title="授权汇总",
                    icon="ep:histogram",
                    rank=50,
                ),
            ],
        ),
    ]
    return [route for route in routes if route is not None]


@router.get("/system/departments", response_model=PageResult[DepartmentItem], summary="分页列出 OA 部门")
async def list_departments(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _: User = Depends(require_permission("system:department:read")),
    session: AsyncSession = Depends(get_session),
) -> PageResult[DepartmentItem]:
    total = await session.scalar(select(func.count()).select_from(Department)) or 0
    departments = (
        await session.scalars(
            select(Department)
            .options(selectinload(Department.roles))
            .order_by(Department.name)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return page_result([department_item(department) for department in departments], page, page_size, total)


@router.get("/system/departments/{department_id}", response_model=DepartmentItem, summary="获取 OA 部门")
async def get_department(
    department_id: str,
    _: User = Depends(require_permission("system:department:read")),
    session: AsyncSession = Depends(get_session),
) -> DepartmentItem:
    return department_item(await department_or_404(session, department_id))


@router.post(
    "/system/departments",
    response_model=DepartmentItem,
    status_code=status.HTTP_201_CREATED,
    summary="创建 OA 部门",
)
async def create_department(
    payload: DepartmentCreate,
    actor: User = Depends(require_permission("system:department:write")),
    session: AsyncSession = Depends(get_session),
) -> DepartmentItem:
    if await session.scalar(select(Department.id).where(Department.name == payload.name)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="department name already exists")
    if payload.parent_id:
        await department_or_404(session, payload.parent_id)
    department = Department(
        id=new_id(), name=payload.name, parent_id=payload.parent_id, is_active=payload.is_active, roles=[]
    )
    if payload.role_codes:
        if "system:authorization:write" not in permission_codes(actor):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="missing permission: system:authorization:write")
        await replace_department_roles(session, department, payload.role_codes)
    session.add(department)
    await write_audit_log(session, actor_id=actor.id, action="system.department.create", outcome="success")
    await session.commit()
    return department_item(department)


@router.patch("/system/departments/{department_id}", response_model=DepartmentItem, summary="更新 OA 部门")
async def update_department(
    department_id: str,
    payload: DepartmentUpdate,
    actor: User = Depends(require_permission("system:department:write")),
    session: AsyncSession = Depends(get_session),
) -> DepartmentItem:
    department = await department_or_404(session, department_id)
    if payload.name is not None and payload.name != department.name:
        if await session.scalar(select(Department.id).where(Department.name == payload.name)):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="department name already exists")
        department.name = payload.name
    if payload.parent_id is not None:
        if payload.parent_id == department.id:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department cannot parent itself")
        if payload.parent_id:
            await department_or_404(session, payload.parent_id)
        department.parent_id = payload.parent_id
    if payload.is_active is not None:
        department.is_active = payload.is_active
    if payload.role_codes is not None:
        if "system:authorization:write" not in permission_codes(actor):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="missing permission: system:authorization:write")
        await replace_department_roles(session, department, payload.role_codes)
    await write_audit_log(session, actor_id=actor.id, action="system.department.update", outcome="success")
    await session.commit()
    return department_item(department)


@router.delete("/system/departments/{department_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除空 OA 部门")
async def delete_department(
    department_id: str,
    actor: User = Depends(require_permission("system:department:write")),
    session: AsyncSession = Depends(get_session),
) -> None:
    department = await department_or_404(session, department_id)
    has_child = await session.scalar(select(Department.id).where(Department.parent_id == department.id))
    has_user = await session.scalar(select(User.id).where(User.department_id == department.id))
    has_personnel = await session.scalar(
        select(Personnel.id).where(Personnel.department_id == department.id)
    )
    has_role = await session.scalar(
        select(department_roles.c.role_id).where(department_roles.c.department_id == department.id)
    )
    if has_child or has_user or has_personnel or has_role:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="department is still in use")
    await session.delete(department)
    await write_audit_log(session, actor_id=actor.id, action="system.department.delete", outcome="success")
    await session.commit()


@router.get("/organization/personnel", response_model=PageResult[PersonnelItem], summary="分页列出部门人员档案")
async def list_personnel(
    q: str = Query(default="", max_length=80),
    department_id: str = Query(default="", max_length=36),
    include_inactive: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _: User = Depends(require_permission("system:personnel:read")),
    session: AsyncSession = Depends(get_session),
) -> PageResult[PersonnelItem]:
    statement = select(Personnel)
    if q.strip():
        keyword = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                Personnel.name.ilike(keyword),
                Personnel.phone.ilike(keyword),
                Personnel.email.ilike(keyword),
            )
        )
    if department_id:
        statement = statement.where(Personnel.department_id == department_id)
    if not include_inactive:
        statement = statement.where(Personnel.is_active.is_(True))
    total = await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    personnel_rows = (await session.scalars(
        statement.order_by(Personnel.name, Personnel.id).offset((page - 1) * page_size).limit(page_size)
    )).all()
    return page_result([personnel_item(personnel) for personnel in personnel_rows], page, page_size, total)


@router.post(
    "/organization/personnel",
    response_model=PersonnelItem,
    status_code=status.HTTP_201_CREATED,
    summary="创建部门人员档案",
)
async def create_personnel(
    payload: PersonnelCreate,
    actor: User = Depends(require_permission("system:personnel:write")),
    session: AsyncSession = Depends(get_session),
) -> PersonnelItem:
    department = await department_or_404(session, payload.department_id)
    if not department.is_active:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department is inactive")
    personnel = Personnel(id=new_id(), **payload.model_dump())
    session.add(personnel)
    await write_audit_log(
        session, actor_id=actor.id, action="organization.personnel.create", outcome="success"
    )
    await session.commit()
    await session.refresh(personnel)
    return personnel_item(personnel)


@router.patch(
    "/organization/personnel/{personnel_id}",
    response_model=PersonnelItem,
    summary="更新部门人员档案",
)
async def update_personnel(
    personnel_id: str,
    payload: PersonnelUpdate,
    actor: User = Depends(require_permission("system:personnel:write")),
    session: AsyncSession = Depends(get_session),
) -> PersonnelItem:
    personnel = await personnel_or_404(session, personnel_id)
    values = payload.model_dump(exclude_unset=True)
    if "department_id" in values:
        department = await department_or_404(session, values["department_id"])
        if not department.is_active:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department is inactive")
    for field, value in values.items():
        setattr(personnel, field, value)
    await write_audit_log(
        session, actor_id=actor.id, action="organization.personnel.update", outcome="success"
    )
    await session.commit()
    await session.refresh(personnel)
    return personnel_item(personnel)


@router.delete(
    "/organization/personnel/{personnel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="停用部门人员档案",
)
async def deactivate_personnel(
    personnel_id: str,
    actor: User = Depends(require_permission("system:personnel:write")),
    session: AsyncSession = Depends(get_session),
) -> None:
    personnel = await personnel_or_404(session, personnel_id)
    if personnel.is_active:
        personnel.is_active = False
        await write_audit_log(
            session,
            actor_id=actor.id,
            action="organization.personnel.deactivate",
            outcome="success",
        )
        await session.commit()


@router.get(
    "/authorization/departments",
    response_model=PageResult[AuthorizationDepartmentItem],
    summary="分页列出部门可分配角色",
)
async def list_authorization_departments(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _: User = Depends(require_permission("system:authorization:read")),
    session: AsyncSession = Depends(get_session),
) -> PageResult[AuthorizationDepartmentItem]:
    total = await session.scalar(select(func.count()).select_from(Department)) or 0
    departments = (await session.scalars(
        select(Department).options(selectinload(Department.roles)).order_by(Department.name)
        .offset((page - 1) * page_size).limit(page_size)
    )).all()
    return page_result(
        [AuthorizationDepartmentItem(**department_item(department).model_dump()) for department in departments],
        page,
        page_size,
        total,
    )


@router.put(
    "/authorization/departments/{department_id}/roles",
    response_model=AuthorizationDepartmentItem,
    summary="设置部门可分配角色",
)
async def update_department_roles(
    department_id: str,
    payload: DepartmentRoleUpdate,
    actor: User = Depends(require_permission("system:authorization:write")),
    session: AsyncSession = Depends(get_session),
) -> AuthorizationDepartmentItem:
    department = await department_or_404(session, department_id)
    roles = await replace_department_roles(session, department, payload.role_codes)
    await write_audit_log(
        session,
        actor_id=actor.id,
        action="authorization.department_roles.update",
        outcome="success",
        detail=f"department_id={department.id};role_codes={','.join(sorted(role.code for role in roles))}",
    )
    await session.commit()
    return AuthorizationDepartmentItem(**department_item(department).model_dump())


@router.get("/authorization/personnel", response_model=PageResult[AuthorizationPersonnelItem], summary="分页列出可授权人员与关联账号")
async def list_authorization_personnel(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _: User = Depends(require_permission("system:authorization:read")),
    session: AsyncSession = Depends(get_session),
) -> PageResult[AuthorizationPersonnelItem]:
    total = await session.scalar(select(func.count()).select_from(Personnel)) or 0
    personnel_rows = (await session.scalars(
        select(Personnel).order_by(Personnel.name, Personnel.id).offset((page - 1) * page_size).limit(page_size)
    )).all()
    user_rows = (
        await session.scalars(
            select(User).options(selectinload(User.roles), selectinload(User.permission_grants))
        )
    ).all()
    users_by_id = {user.id: user for user in user_rows}
    return page_result(
        [authorization_personnel_item(personnel, users_by_id.get(personnel.user_id)) for personnel in personnel_rows],
        page,
        page_size,
        total,
    )


@router.put(
    "/authorization/personnel/{personnel_id}/account",
    response_model=AuthorizationPersonnelItem,
    summary="关联人员档案与 OA 登录账号",
)
async def update_personnel_account_link(
    personnel_id: str,
    payload: PersonnelAccountLinkUpdate,
    actor: User = Depends(require_permission("system:authorization:write")),
    session: AsyncSession = Depends(get_session),
) -> AuthorizationPersonnelItem:
    personnel = await personnel_or_404(session, personnel_id)
    previous_user_id = personnel.user_id
    if payload.user_id is None:
        personnel.user_id = None
        if previous_user_id:
            previous_user = await user_or_404(session, previous_user_id)
            previous_user.permission_grants = []
            previous_user.token_version += 1
            await revoke_user_sessions(session, previous_user.id)
        user: User | None = None
    else:
        user = await user_or_404(session, payload.user_id)
        if user.department_id != personnel.department_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="personnel and OA account must belong to the same department",
            )
        linked_personnel = await session.scalar(
            select(Personnel).where(Personnel.user_id == user.id, Personnel.id != personnel.id)
        )
        if linked_personnel is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="OA account is already linked to another personnel record",
            )
        personnel.user_id = user.id
    await write_audit_log(
        session,
        actor_id=actor.id,
        action="authorization.personnel_account_link.update",
        outcome="success",
        detail=f"personnel_id={personnel.id};user_id={personnel.user_id or ''}",
    )
    await session.commit()
    return authorization_personnel_item(personnel, user)


@router.put(
    "/authorization/personnel/{personnel_id}/permission-grants",
    response_model=AuthorizationPersonnelItem,
    summary="设置人员例外权限",
)
async def update_personnel_permission_grants(
    personnel_id: str,
    payload: PersonnelPermissionGrantUpdate,
    actor: User = Depends(require_permission("system:authorization:write")),
    session: AsyncSession = Depends(get_session),
) -> AuthorizationPersonnelItem:
    personnel = await personnel_or_404(session, personnel_id)
    if not personnel.user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="personnel must link an OA account before granting permissions",
        )
    user = await user_or_404(session, personnel.user_id)
    permissions = await resolve_permissions(session, payload.permission_codes)
    user.permission_grants = permissions
    user.token_version += 1
    await revoke_user_sessions(session, user.id)
    await write_audit_log(
        session,
        actor_id=actor.id,
        action="authorization.personnel_permission_grants.update",
        outcome="success",
        detail=f"personnel_id={personnel.id};permission_codes={','.join(sorted(permission.code for permission in permissions))}",
    )
    await session.commit()
    return authorization_personnel_item(personnel, user)


@router.get(
    "/authorization/personnel/{personnel_id}/effective-access",
    response_model=EffectiveAccessItem,
    summary="查询人员账号的最终权限来源",
)
async def get_personnel_effective_access(
    personnel_id: str,
    _: User = Depends(require_permission("system:authorization:read")),
    session: AsyncSession = Depends(get_session),
) -> EffectiveAccessItem:
    personnel = await personnel_or_404(session, personnel_id)
    if not personnel.user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="personnel has no linked OA account",
        )
    user = await user_or_404(session, personnel.user_id)
    role_sources = [
        EffectivePermissionSource(code=permission.code, source="role", role_code=role.code)
        for role in user.roles
        if role.is_active
        for permission in role.permissions
    ]
    direct_sources = [
        EffectivePermissionSource(code=permission.code, source="direct")
        for permission in user.permission_grants
    ]
    return EffectiveAccessItem(
        personnel_id=personnel.id,
        user_id=user.id,
        username=user.username,
        department_id=personnel.department_id,
        department_role_codes=await department_role_codes(session, personnel.department_id),
        assigned_role_codes=sorted(role.code for role in user.roles if role.is_active),
        direct_permission_codes=sorted(permission.code for permission in user.permission_grants),
        effective_permissions=sorted(
            [*role_sources, *direct_sources], key=lambda item: (item.code, item.source, item.role_code)
        ),
    )


@router.get("/system/permissions", response_model=PageResult[PermissionItem], summary="分页列出 OA 权限点")
async def list_permissions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    kind: str = Query(default="", pattern=r"^(|page|action)$"),
    _: User = Depends(require_permission("system:permission:read")),
    session: AsyncSession = Depends(get_session),
) -> PageResult[PermissionItem]:
    statement = select(Permission)
    if kind:
        statement = statement.where(Permission.kind == kind)
    total = await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    permissions = (await session.scalars(statement.order_by(Permission.code).offset((page - 1) * page_size).limit(page_size))).all()
    return page_result([permission_item(permission) for permission in permissions], page, page_size, total)


@router.get("/system/permissions/{permission_id}", response_model=PermissionItem, summary="获取 OA 权限点")
async def get_permission(
    permission_id: str,
    _: User = Depends(require_permission("system:permission:read")),
    session: AsyncSession = Depends(get_session),
) -> PermissionItem:
    return permission_item(await permission_or_404(session, permission_id))


@router.post(
    "/system/permissions",
    response_model=PermissionItem,
    status_code=status.HTTP_201_CREATED,
    summary="创建 OA 权限点",
)
async def create_permission(
    payload: PermissionCreate,
    actor: User = Depends(require_permission("system:permission:write")),
    session: AsyncSession = Depends(get_session),
) -> PermissionItem:
    if await session.scalar(select(Permission.id).where(Permission.code == payload.code)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="permission code already exists")
    permission = Permission(id=new_id(), code=payload.code, name=payload.name, kind=payload.kind)
    session.add(permission)
    await write_audit_log(session, actor_id=actor.id, action="system.permission.create", outcome="success")
    await session.commit()
    return permission_item(permission)


@router.patch("/system/permissions/{permission_id}", response_model=PermissionItem, summary="更新 OA 权限点")
async def update_permission(
    permission_id: str,
    payload: PermissionUpdate,
    actor: User = Depends(require_permission("system:permission:write")),
    session: AsyncSession = Depends(get_session),
) -> PermissionItem:
    permission = await permission_or_404(session, permission_id)
    if payload.name is not None:
        permission.name = payload.name
    if payload.kind is not None:
        permission.kind = payload.kind
    await write_audit_log(session, actor_id=actor.id, action="system.permission.update", outcome="success")
    await session.commit()
    return permission_item(permission)


@router.delete("/system/permissions/{permission_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除未分配 OA 权限点")
async def delete_permission(
    permission_id: str,
    actor: User = Depends(require_permission("system:permission:write")),
    session: AsyncSession = Depends(get_session),
) -> None:
    permission = await permission_or_404(session, permission_id)
    assigned = await session.scalar(
        select(role_permissions.c.role_id).where(role_permissions.c.permission_id == permission.id)
    )
    granted = await session.scalar(
        select(user_permission_grants.c.user_id).where(
            user_permission_grants.c.permission_id == permission.id
        )
    )
    if assigned or granted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="permission is assigned to a role")
    await session.delete(permission)
    await write_audit_log(session, actor_id=actor.id, action="system.permission.delete", outcome="success")
    await session.commit()


@router.get("/system/roles", response_model=PageResult[RoleItem], summary="分页列出 OA 角色")
async def list_roles(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _: User = Depends(require_permission("system:role:read")),
    session: AsyncSession = Depends(get_session),
) -> PageResult[RoleItem]:
    total = await session.scalar(select(func.count()).select_from(Role)) or 0
    roles = (
        await session.scalars(select(Role).options(selectinload(Role.permissions)).order_by(Role.code).offset((page - 1) * page_size).limit(page_size))
    ).all()
    return page_result([role_item(role) for role in roles], page, page_size, total)


@router.get("/system/roles/{role_id}", response_model=RoleItem, summary="获取 OA 角色")
async def get_role(
    role_id: str,
    _: User = Depends(require_permission("system:role:read")),
    session: AsyncSession = Depends(get_session),
) -> RoleItem:
    return role_item(await role_or_404(session, role_id))


@router.post(
    "/system/roles",
    response_model=RoleItem,
    status_code=status.HTTP_201_CREATED,
    summary="创建 OA 角色",
)
async def create_role(
    payload: RoleCreate,
    actor: User = Depends(require_permission("system:role:write")),
    session: AsyncSession = Depends(get_session),
) -> RoleItem:
    if await session.scalar(select(Role.id).where(Role.code == payload.code)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="role code already exists")
    role = Role(
        id=new_id(),
        code=payload.code,
        name=payload.name,
        data_scope=payload.data_scope,
        is_active=payload.is_active,
        permissions=await resolve_permissions(session, payload.permission_codes),
    )
    session.add(role)
    await write_audit_log(session, actor_id=actor.id, action="system.role.create", outcome="success")
    await session.commit()
    return role_item(role)


@router.patch("/system/roles/{role_id}", response_model=RoleItem, summary="更新 OA 角色及权限")
async def update_role(
    role_id: str,
    payload: RoleUpdate,
    actor: User = Depends(require_permission("system:role:write")),
    session: AsyncSession = Depends(get_session),
) -> RoleItem:
    role = await role_or_404(session, role_id)
    if payload.name is not None:
        role.name = payload.name
    if payload.data_scope is not None:
        role.data_scope = payload.data_scope
    if payload.is_active is not None:
        role.is_active = payload.is_active
    if payload.permission_codes is not None:
        role.permissions = await resolve_permissions(session, payload.permission_codes)
    await write_audit_log(session, actor_id=actor.id, action="system.role.update", outcome="success")
    await session.commit()
    return role_item(role)


@router.delete("/system/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除未分配 OA 角色")
async def delete_role(
    role_id: str,
    actor: User = Depends(require_permission("system:role:write")),
    session: AsyncSession = Depends(get_session),
) -> None:
    role = await role_or_404(session, role_id)
    assigned = await session.scalar(select(user_roles.c.user_id).where(user_roles.c.role_id == role.id))
    enabled_for_department = await session.scalar(
        select(department_roles.c.department_id).where(department_roles.c.role_id == role.id)
    )
    if assigned or enabled_for_department:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="role is assigned to an OA user")
    await session.delete(role)
    await write_audit_log(session, actor_id=actor.id, action="system.role.delete", outcome="success")
    await session.commit()


@router.get("/system/users", response_model=PageResult[OaUserItem], summary="分页列出 OA 员工账号")
async def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _: User = Depends(require_permission("system:user:read")),
    session: AsyncSession = Depends(get_session),
) -> PageResult[OaUserItem]:
    total = await session.scalar(select(func.count()).select_from(User)) or 0
    users = (
        await session.scalars(
            select(User).options(selectinload(User.roles)).order_by(User.username).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    return page_result([oa_user_item(user) for user in users], page, page_size, total)


@router.get("/system/users/{user_id}", response_model=OaUserItem, summary="获取 OA 员工账号")
async def get_oa_user(
    user_id: str,
    _: User = Depends(require_permission("system:user:read")),
    session: AsyncSession = Depends(get_session),
) -> OaUserItem:
    return oa_user_item(await user_or_404(session, user_id))


@router.post(
    "/system/users",
    response_model=OaUserItem,
    status_code=status.HTTP_201_CREATED,
    summary="创建 OA 员工账号",
)
async def create_oa_user(
    payload: OaUserCreate,
    actor: User = Depends(require_permission("system:user:write")),
    session: AsyncSession = Depends(get_session),
) -> OaUserItem:
    if await session.scalar(select(User.id).where(User.username == payload.username)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="OA username already exists")
    department = await department_or_404(session, payload.department_id)
    if not department.is_active:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department is inactive")
    roles = await resolve_roles(session, payload.role_codes)
    await assert_roles_enabled_for_department(session, department.id, roles)
    user = User(
        id=new_id(),
        username=payload.username,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        department_id=department.id,
        roles=roles,
        is_active=payload.is_active,
    )
    session.add(user)
    await write_audit_log(session, actor_id=actor.id, action="system.user.create", outcome="success")
    await session.commit()
    return oa_user_item(user)


@router.patch("/system/users/{user_id}", response_model=OaUserItem, summary="更新 OA 员工账号")
async def update_oa_user(
    user_id: str,
    payload: OaUserUpdate,
    actor: User = Depends(require_permission("system:user:write")),
    session: AsyncSession = Depends(get_session),
) -> OaUserItem:
    user = await user_or_404(session, user_id)
    if payload.is_active is False and user.id == actor.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cannot deactivate current OA account")
    security_changed = False
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.department_id is not None:
        department = await department_or_404(session, payload.department_id)
        if not department.is_active:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department is inactive")
        user.department_id = department.id
    target_department_id = payload.department_id or user.department_id
    if payload.is_active is not None and payload.is_active != user.is_active:
        user.is_active = payload.is_active
        security_changed = True
    if payload.role_codes is not None:
        roles = await resolve_roles(session, payload.role_codes)
        await assert_roles_enabled_for_department(session, target_department_id, roles)
        user.roles = roles
        security_changed = True
    elif payload.department_id is not None:
        await assert_roles_enabled_for_department(session, target_department_id, user.roles)
    if security_changed:
        user.token_version += 1
        await revoke_user_sessions(session, user.id)
    await write_audit_log(session, actor_id=actor.id, action="system.user.update", outcome="success")
    await session.commit()
    return oa_user_item(user)


@router.put("/system/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT, summary="重置 OA 员工口令")
async def reset_oa_user_password(
    user_id: str,
    payload: OaUserPasswordReset,
    actor: User = Depends(require_permission("system:user:write")),
    session: AsyncSession = Depends(get_session),
) -> None:
    user = await user_or_404(session, user_id)
    user.password_hash = hash_password(payload.password)
    user.token_version += 1
    await revoke_user_sessions(session, user.id)
    await write_audit_log(session, actor_id=actor.id, action="system.user.reset_password", outcome="success")
    await session.commit()


@router.delete(
    "/system/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="停用 OA 员工账号",
)
async def delete_oa_user(
    user_id: str,
    actor: User = Depends(require_permission("system:user:write")),
    session: AsyncSession = Depends(get_session),
) -> None:
    """逻辑删除账号，保留审计轨迹，避免删除权限变更的责任链。"""
    user = await user_or_404(session, user_id)
    if user.id == actor.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="cannot delete current OA account"
        )
    if user.is_active:
        user.is_active = False
        user.token_version += 1
        await revoke_user_sessions(session, user.id)
        await write_audit_log(
            session, actor_id=actor.id, action="system.user.delete", outcome="success"
        )
        await session.commit()
