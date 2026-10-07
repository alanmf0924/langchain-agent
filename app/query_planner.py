"""V1 的确定性意图识别与检索计划。

规则负责路由、追问和安全边界；模型只能依据已筛选证据组织回答。
"""

from __future__ import annotations

from app.models import QueryPlan

SAFETY_WORDS = ("疼", "破溃", "流脓", "严重红肿", "持续不适")
KNOWLEDGE_WORDS = ("怎么用", "用法", "成分", "适合", "注意", "能不能", "可不可以")
NEED_WORDS = (
    "干", "紧", "紧绷", "保湿", "修护", "换季", "泛红", "敏感", "美白", "焕亮", "暗沉",
    "油", "油光", "出油", "毛孔", "控油", "闷",
)
CONSTRAINT_WORDS = ("预算", "元", "a 酸", "刷酸", "正在用", "过敏", "避开", "不想")
VAGUE_QUESTIONS = ("买什么", "怎么护肤", "推荐一下", "怎么办", "护肤品")
# 商城履约问题必须优先于“怎么用”等商品知识词判断，避免“订单怎么查询”被误判。
COMMERCE_SERVICE_WORDS = ("订单", "物流", "快递", "发货", "退款", "退货", "支付", "售后")


def plan_question(question: str) -> QueryPlan:
    """把用户表达转换为公开的业务检索计划，而不是输出模型思维链。"""
    normalized = question.lower().strip()
    if any(word in normalized for word in SAFETY_WORDS):
        return QueryPlan(intent="safety", sub_questions=[], sources=[], next_action="stop")

    if any(word in normalized for word in COMMERCE_SERVICE_WORDS):
        return QueryPlan(intent="out_of_scope", sub_questions=[], sources=[], next_action="stop")

    if len(normalized) <= 5 or any(normalized == item for item in VAGUE_QUESTIONS):
        return QueryPlan(intent="clarify", sub_questions=[], sources=[], next_action="clarify")

    has_need = any(word in normalized for word in NEED_WORDS)
    has_knowledge = any(word in normalized for word in KNOWLEDGE_WORDS)
    has_constraint = any(word in normalized for word in CONSTRAINT_WORDS)
    # 只有不存在预算、既有使用或避开条件时，才把问题当作单纯产品知识；
    # 否则“成分顾虑 + 选购”仍需要进入复杂推荐的拆解流程。
    if has_knowledge and not has_constraint:
        return QueryPlan(
            intent="product_knowledge",
            sub_questions=["确认用户询问的商品或知识点", "检索该商品的已审核资料"],
            sources=["search_catalog", "retrieve_evidence"],
            next_action="answer",
        )
    if not has_need:
        # 订单、物流及泛闲聊不属于护肤选购问答。这里必须在任何检索和模型调用前停止，
        # 不能把无关问题伪装成“请补充肤感”的追问，更不能因此进入推荐链路。
        return QueryPlan(intent="out_of_scope", sub_questions=[], sources=[], next_action="stop")

    sub_questions = ["识别肤感与护理诉求", "筛选审核通过且在售的候选商品"]
    if has_constraint:
        sub_questions.append("校验预算、现有使用与避开条件")
    # “避开某成分”是硬约束。资料未列成分时必须显式降级，不能把未知当成不含。
    if "避开" in normalized or "不含" in normalized:
        sub_questions.append("核验候选商品的成分资料是否足以满足避开条件")
    sub_questions.append("确认候选商品的实时价格与库存")
    return QueryPlan(
        intent="recommendation",
        sub_questions=sub_questions,
        sources=["search_catalog", "get_realtime_price_stock", "retrieve_evidence"],
        next_action="answer",
    )
