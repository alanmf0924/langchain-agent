"""OA 中可分配的明确权限资源。

权限码的三个字段含义固定为 ``业务域:资源:动作``。页面能否进入由对应的
``read`` 权限决定；页面内按钮由 ``write`` 权限决定；数据范围不写进权限码，
而是由角色的 ``data_scope`` 独立控制，避免把三个维度混成一个通配符。
"""

from __future__ import annotations

from typing import Final

PermissionDefinition = tuple[str, str, str]

PERMISSION_DEFINITIONS: Final[tuple[PermissionDefinition, ...]] = (
    ("catalog:product:read", "查看商品", "page"),
    ("catalog:product:write", "维护商品", "action"),
    ("customer:profile:read", "查看前台客户", "page"),
    ("order:record:read", "查看商城订单", "page"),
    ("order:record:write", "处理商城订单", "action"),
    ("system:user:read", "查看登录账号", "page"),
    ("system:user:write", "维护登录账号", "action"),
    ("system:department:read", "查看部门", "page"),
    ("system:department:write", "维护部门", "action"),
    ("system:personnel:read", "查看人员档案", "page"),
    ("system:personnel:write", "维护人员档案", "action"),
    ("system:authorization:read", "查看组织授权", "page"),
    ("system:authorization:write", "维护组织授权", "action"),
    ("system:permission:read", "查看权限资源", "page"),
    ("system:permission:write", "维护权限资源", "action"),
    ("system:role:read", "查看角色", "page"),
    ("system:role:write", "维护角色", "action"),
    ("assistant:knowledge:read", "查看知识库", "page"),
    ("assistant:knowledge:write", "维护知识库", "action"),
    ("assistant:operations:read", "查看助手运行审计", "page"),
    ("assistant:evaluation:read", "查看评测报告", "page"),
    ("assistant:evaluation:write", "执行评测", "action"),
)

ALL_PERMISSION_CODES: Final[frozenset[str]] = frozenset(
    code for code, _, _ in PERMISSION_DEFINITIONS
)
