from __future__ import annotations

import re
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

PermissionKind = Literal["page", "action"]
DataScope = Literal["all", "department", "self"]
PersonnelGender = Literal["female", "male", "unspecified"]
PageItem = TypeVar("PageItem")


class PageResult(BaseModel, Generic[PageItem]):
    """统一的服务端分页契约；选择框另走小型 options 接口。"""

    items: list[PageItem]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=128)


class CurrentUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    username: str
    display_name: str
    role_codes: list[str]
    permission_codes: list[str]


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    user: CurrentUserResponse


class UserListItem(BaseModel):
    id: str
    username: str
    display_name: str
    is_active: bool
    role_codes: list[str]


class DepartmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    parent_id: str = ""
    is_active: bool = True
    role_codes: list[str] = Field(default_factory=list, max_length=100)


class DepartmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    parent_id: str | None = None
    is_active: bool | None = None
    role_codes: list[str] | None = Field(default=None, max_length=100)


class DepartmentItem(BaseModel):
    id: str
    name: str
    parent_id: str
    is_active: bool
    role_codes: list[str] = Field(default_factory=list)


class PersonnelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(default="", max_length=32)
    email: str = Field(default="", max_length=254)
    gender: PersonnelGender = "unspecified"
    department_id: str = Field(min_length=1, max_length=36)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and not re.fullmatch(r"[0-9+() -]+", normalized):
            raise ValueError("phone format is invalid")
        return normalized

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
            raise ValueError("email format is invalid")
        return normalized


class PersonnelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    gender: PersonnelGender | None = None
    department_id: str | None = Field(default=None, min_length=1, max_length=36)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_optional_name(cls, value: str | None) -> str | None:
        return PersonnelCreate.normalize_name(value) if value is not None else None

    @field_validator("phone")
    @classmethod
    def validate_optional_phone(cls, value: str | None) -> str | None:
        return PersonnelCreate.validate_phone(value) if value is not None else None

    @field_validator("email")
    @classmethod
    def validate_optional_email(cls, value: str | None) -> str | None:
        return PersonnelCreate.validate_email(value) if value is not None else None


class PersonnelItem(BaseModel):
    id: str
    name: str
    phone: str
    email: str
    gender: PersonnelGender
    department_id: str
    is_active: bool


class DepartmentRoleUpdate(BaseModel):
    role_codes: list[str] = Field(default_factory=list, max_length=100)


class AuthorizationDepartmentItem(DepartmentItem):
    role_codes: list[str]


class PersonnelAccountLinkUpdate(BaseModel):
    user_id: str | None = Field(default=None, max_length=36)


class PersonnelPermissionGrantUpdate(BaseModel):
    permission_codes: list[str] = Field(default_factory=list, max_length=500)


class AuthorizationPersonnelItem(BaseModel):
    id: str
    name: str
    department_id: str
    is_active: bool
    user_id: str | None
    username: str
    display_name: str
    assigned_role_codes: list[str]
    direct_permission_codes: list[str]


class EffectivePermissionSource(BaseModel):
    code: str
    source: Literal["role", "direct"]
    role_code: str = ""


class EffectiveAccessItem(BaseModel):
    personnel_id: str
    user_id: str
    username: str
    department_id: str
    department_role_codes: list[str]
    assigned_role_codes: list[str]
    direct_permission_codes: list[str]
    effective_permissions: list[EffectivePermissionSource]


class PermissionCreate(BaseModel):
    code: str = Field(
        min_length=3,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_]*(?::[a-z0-9_]+){1,3}$",
    )
    name: str = Field(min_length=1, max_length=80)
    kind: PermissionKind = "action"


class PermissionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    kind: PermissionKind | None = None


class PermissionItem(BaseModel):
    id: str
    code: str
    name: str
    kind: PermissionKind


class RoleCreate(BaseModel):
    code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=80)
    data_scope: DataScope = "self"
    permission_codes: list[str] = Field(default_factory=list)
    is_active: bool = True


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    data_scope: DataScope | None = None
    permission_codes: list[str] | None = None
    is_active: bool | None = None


class RoleItem(BaseModel):
    id: str
    code: str
    name: str
    data_scope: DataScope
    is_active: bool
    permission_codes: list[str]


class OaUserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)
    department_id: str
    role_codes: list[str] = Field(default_factory=list)
    is_active: bool = True


class OaUserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    department_id: str | None = None
    role_codes: list[str] | None = None
    is_active: bool | None = None


class OaUserPasswordReset(BaseModel):
    password: str = Field(min_length=12, max_length=128)


class OaUserItem(UserListItem):
    department_id: str


class RouteMeta(BaseModel):
    title: str
    icon: str
    rank: int
    roles: list[str]
    showLink: bool = True
    keepAlive: bool = False
    auths: list[str] = Field(default_factory=list)


class RouteItem(BaseModel):
    path: str
    component: str | None = None
    name: str | None = None
    redirect: str | None = None
    meta: RouteMeta
    # Pure Admin 的叶子页面不携带 children；返回 [] 会被侧栏当成空目录过滤。
    children: list[RouteItem] | None = None


ProductSku = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
ProductTags = Annotated[list[str], Field(max_length=12)]


class CatalogProductCreate(BaseModel):
    sku_id: ProductSku
    title: str = Field(min_length=1, max_length=160)
    spec: str = Field(default="", max_length=120)
    price_fen: int = Field(ge=0, le=100_000_000)
    stock: int = Field(ge=0, le=10_000_000)
    description: str = Field(default="", max_length=20_000)
    manual: str = Field(default="", max_length=20_000)
    ingredients: list[str] = Field(default_factory=list, max_length=500)
    ingredient_disclosure_complete: bool = False
    usage: str = Field(default="", max_length=4_000)
    cautions: str = Field(default="", max_length=4_000)
    category_code: str = Field(default="", max_length=64)
    scenario_tags: ProductTags = Field(default_factory=list)
    on_sale: bool = False
    assistant_approved: bool = False
    image_ids: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("ingredients", "scenario_tags")
    @classmethod
    def normalize_text_lists(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("list values must not repeat")
        if any(len(value) > 200 for value in normalized):
            raise ValueError("list value is too long")
        return normalized

    @field_validator("image_ids")
    @classmethod
    def unique_image_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("image_ids must not repeat")
        return values


class CatalogProductUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    spec: str | None = Field(default=None, max_length=120)
    price_fen: int | None = Field(default=None, ge=0, le=100_000_000)
    stock: int | None = Field(default=None, ge=0, le=10_000_000)
    description: str | None = Field(default=None, max_length=20_000)
    manual: str | None = Field(default=None, max_length=20_000)
    ingredients: list[str] | None = Field(default=None, max_length=500)
    ingredient_disclosure_complete: bool | None = None
    usage: str | None = Field(default=None, max_length=4_000)
    cautions: str | None = Field(default=None, max_length=4_000)
    category_code: str | None = Field(default=None, max_length=64)
    scenario_tags: ProductTags | None = None
    on_sale: bool | None = None
    assistant_approved: bool | None = None
    image_ids: list[str] | None = Field(default=None, max_length=12)
    expected_revision: int = Field(ge=1)

    @field_validator("ingredients", "scenario_tags")
    @classmethod
    def normalize_optional_text_lists(cls, values: list[str] | None) -> list[str] | None:
        return CatalogProductCreate.normalize_text_lists(values) if values is not None else None

    @field_validator("image_ids")
    @classmethod
    def unique_optional_image_ids(cls, values: list[str] | None) -> list[str] | None:
        return CatalogProductCreate.unique_image_ids(values) if values is not None else None


class CatalogImageItem(BaseModel):
    id: str
    url: str
    content_type: str
    size_bytes: int
    sort_order: int


class CatalogProductItem(BaseModel):
    id: str
    sku_id: str
    title: str
    spec: str
    price_fen: int
    stock: int
    description: str
    manual: str
    ingredients: list[str]
    ingredient_disclosure_complete: bool
    usage: str
    cautions: str
    category_code: str
    scenario_tags: list[str]
    on_sale: bool
    assistant_approved: bool
    revision: int
    image_urls: list[str]
    images: list[CatalogImageItem]
