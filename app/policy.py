from dataclasses import dataclass
from typing import Literal

Decision = Literal["allow", "require_confirmation", "deny"]


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reason: str


# 只读查询可以自动执行，但仍会在 service.py 中审计实际调用结果。
READ_ONLY_TOOLS = {"search_catalog", "retrieve_evidence", "get_realtime_price_stock"}
# 交易草稿并非最终下单，但会产生用户购物意图，因此必须等待明确确认。
CONFIRMATION_TOOLS = {"create_cart_draft"}
# 高风险业务动作不向模型暴露；即使模型产生同名调用意图也会被拒绝。
DENIED_TOOLS = {
    "grant_points",
    "change_order_paid_status",
    "modify_product_price",
    "modify_inventory",
}


def decide(action: str, confirmed: bool = False) -> PolicyDecision:
    """唯一的工具授权入口，采用默认拒绝，不能由模型提示词绕过。"""
    if action in READ_ONLY_TOOLS:
        return PolicyDecision("allow", "只读查询可自动执行")
    if action in DENIED_TOOLS:
        return PolicyDecision("deny", "该交易或运营操作不允许 Agent 调用")
    if action in CONFIRMATION_TOOLS and not confirmed:
        return PolicyDecision("require_confirmation", "创建交易草稿前必须得到用户明确确认")
    if action in CONFIRMATION_TOOLS:
        return PolicyDecision("allow", "用户已确认，仍需执行前二次校验")
    # 新增 Tool 前必须在上方显式分类，防止遗漏时获得默认权限。
    return PolicyDecision("deny", "未登记的 Tool 默认拒绝")
