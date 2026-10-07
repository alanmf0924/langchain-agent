"""显式的 LangGraph 工作流：推荐与确认后的草稿创建分开建模。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.models import AgentResult, Bundle, ModelUsage, QueryPlan
from app.policy import decide

if TYPE_CHECKING:
    from app.service import SkinAssistantService


class RecommendationState(TypedDict, total=False):
    """一次推荐运行在节点间传递的显式状态；只允许节点补充，不信任前端传入的中间字段。"""

    question: str
    run_id: str
    query_plan: QueryPlan
    candidates: list[dict[str, Any]]
    constraint_data_complete: bool
    available_products: list[dict[str, Any]]
    bundle: Bundle
    evidence: list[dict[str, Any]]
    answer: str
    model_usage: ModelUsage
    model_call_count: int
    result: AgentResult


class CartDraftState(TypedDict, total=False):
    """用户确认后创建购物车草稿时传递的状态；令牌解析出的 bundle 才是可信来源。"""

    confirmation_token: str
    confirmed: bool
    bundle: Bundle
    available_products: list[dict[str, Any]]
    cart_draft_id: str


def build_recommendation_graph(service: SkinAssistantService):
    """构建 V1 问答图；意图与检索路由在模型调用前由确定性规则决定。"""

    workflow = StateGraph(RecommendationState)

    def initialize_run(state: RecommendationState) -> dict[str, str | int]:
        """Studio 调试时只需提交问题；FastAPI 可继续传入自己的 run_id。"""
        return {"run_id": state.get("run_id", f"run_{uuid4().hex}"), "model_call_count": 0}

    def analyze_intent(state: RecommendationState) -> dict[str, QueryPlan]:
        return {"query_plan": service.build_query_plan(state["question"])}

    def route_after_intent(state: RecommendationState) -> str:
        # 安全、追问和检索路径都在模型调用前固定，模型不能绕过边界。
        plan = state["query_plan"]
        if plan.intent == "safety":
            return "safety_response"
        if plan.intent == "out_of_scope":
            return "out_of_scope_response"
        if plan.next_action == "clarify":
            return "clarify_response"
        return "search_catalog"

    def safety_response(state: RecommendationState) -> dict[str, AgentResult]:
        return {"result": service.safety_result(state["query_plan"])}

    def clarify_response(state: RecommendationState) -> dict[str, AgentResult]:
        return {"result": service.clarify_result(state["query_plan"])}

    def out_of_scope_response(state: RecommendationState) -> dict[str, AgentResult]:
        """范围外问题直接收口，禁止落入目录检索或推荐节点。"""
        return {
            "result": service.out_of_scope_result(state["query_plan"], state["question"])
        }

    def search_catalog(state: RecommendationState) -> dict[str, Any]:
        # 节点通过 Service 调工具，因而同时经过 policy 和 audit。
        candidates = service._tool(
            "search_catalog", {"question": state["question"]}, state["run_id"]
        )
        filtered, constraint_data_complete = service.filter_candidates_for_constraints(
            state["question"], candidates
        )
        return {"candidates": filtered, "constraint_data_complete": constraint_data_complete}

    def route_after_catalog(state: RecommendationState) -> str:
        if not state.get("constraint_data_complete", True):
            return "incomplete_product_info_response"
        if not state["candidates"]:
            return "no_match_response"
        if state["query_plan"].intent == "product_knowledge":
            return "retrieve_evidence"
        return "check_stock"

    def check_stock(state: RecommendationState) -> dict[str, list[dict[str, Any]]]:
        # 候选商品不等于可售商品；库存结果是组套餐和价格展示的唯一来源。
        return {
            "available_products": service._tool(
                "get_realtime_price_stock",
                {"sku_ids": [item["sku_id"] for item in state["candidates"]]},
                state["run_id"],
            )
        }

    def route_after_stock(state: RecommendationState) -> str:
        return (
            "build_bundle"
            if service.has_available_bundle(state["question"], state["available_products"])
            else "no_match_response"
        )

    def build_bundle(state: RecommendationState) -> dict[str, Bundle]:
        # 仅在 route_after_stock 已确认完整可售时执行，避免不完整套餐进入模型。
        return {"bundle": service._bundle(state["question"], state["available_products"])}

    def retrieve_evidence(state: RecommendationState) -> dict[str, list[dict[str, Any]]]:
        # 模型只接收选中商品的审核资料，不接收原始全量商品和知识库。
        sku_ids = (
            [item.sku_id for item in state["bundle"].items]
            if "bundle" in state
            else [item["sku_id"] for item in state["candidates"]]
        )
        return {
            "evidence": service._tool(
                "retrieve_evidence",
                {"question": state["question"], "sku_ids": sku_ids},
                state["run_id"],
            )
        }

    def route_after_evidence(state: RecommendationState) -> str:
        if not state["evidence"]:
            return "no_match_response"
        return (
            "compose_knowledge_answer"
            if state["query_plan"].intent == "product_knowledge"
            else "compose_recommendation_answer"
        )

    def compose_recommendation_answer(state: RecommendationState) -> dict[str, Any]:
        # 这是图中唯一外部模型节点；它只能产出文案，不能修改路由、价格或库存。
        generated = service.compose_answer(
            state["question"], state["evidence"], state.get("model_call_count", 0)
        )
        return {
            "answer": generated.text,
            "model_usage": generated.usage,
            "model_call_count": generated.usage.model_call_count,
        }

    def compose_knowledge_answer(state: RecommendationState) -> dict[str, Any]:
        generated = service.compose_answer(
            state["question"], state["evidence"], state.get("model_call_count", 0)
        )
        return {
            "answer": generated.text,
            "model_usage": generated.usage,
            "model_call_count": generated.usage.model_call_count,
        }

    def prepare_recommendation_result(state: RecommendationState) -> dict[str, AgentResult]:
        return {
            "result": service.prepare_recommendation_result(
                state["bundle"],
                state["answer"],
                state["evidence"],
                state["run_id"],
                state["query_plan"],
                state["model_usage"],
                state["question"],
            )
        }

    def prepare_knowledge_result(state: RecommendationState) -> dict[str, AgentResult]:
        return {
            "result": service.prepare_knowledge_result(
                state["answer"], state["evidence"], state["query_plan"], state["model_usage"]
            )
        }

    def no_match_response(state: RecommendationState) -> dict[str, AgentResult]:
        return {"result": service.no_match_result(state["query_plan"], state["question"])}

    def incomplete_product_info_response(state: RecommendationState) -> dict[str, AgentResult]:
        return {
            "result": service.incomplete_product_info_result(
                state["query_plan"], state["question"]
            )
        }

    workflow.add_node("initialize_run", initialize_run)
    workflow.add_node("analyze_intent", analyze_intent)
    workflow.add_node("safety_response", safety_response)
    workflow.add_node("clarify_response", clarify_response)
    workflow.add_node("out_of_scope_response", out_of_scope_response)
    workflow.add_node("search_catalog", search_catalog)
    workflow.add_node("check_stock", check_stock)
    workflow.add_node("build_bundle", build_bundle)
    workflow.add_node("retrieve_evidence", retrieve_evidence)
    workflow.add_node("compose_recommendation_answer", compose_recommendation_answer)
    workflow.add_node("compose_knowledge_answer", compose_knowledge_answer)
    workflow.add_node("prepare_recommendation_result", prepare_recommendation_result)
    workflow.add_node("prepare_knowledge_result", prepare_knowledge_result)
    workflow.add_node("no_match_response", no_match_response)
    workflow.add_node("incomplete_product_info_response", incomplete_product_info_response)
    workflow.add_edge(START, "initialize_run")
    workflow.add_edge("initialize_run", "analyze_intent")
    workflow.add_conditional_edges(
        "analyze_intent",
        route_after_intent,
        {
            "safety_response": "safety_response",
            "clarify_response": "clarify_response",
            "out_of_scope_response": "out_of_scope_response",
            "search_catalog": "search_catalog",
        },
    )
    workflow.add_edge("safety_response", END)
    workflow.add_edge("clarify_response", END)
    workflow.add_edge("out_of_scope_response", END)
    workflow.add_conditional_edges(
        "search_catalog",
        route_after_catalog,
        {
            "check_stock": "check_stock",
            "retrieve_evidence": "retrieve_evidence",
            "no_match_response": "no_match_response",
            "incomplete_product_info_response": "incomplete_product_info_response",
        },
    )
    workflow.add_conditional_edges(
        "check_stock",
        route_after_stock,
        {"build_bundle": "build_bundle", "no_match_response": "no_match_response"},
    )
    workflow.add_edge("build_bundle", "retrieve_evidence")
    workflow.add_conditional_edges(
        "retrieve_evidence",
        route_after_evidence,
        {
            "compose_recommendation_answer": "compose_recommendation_answer",
            "compose_knowledge_answer": "compose_knowledge_answer",
            "no_match_response": "no_match_response",
        },
    )
    workflow.add_edge("compose_recommendation_answer", "prepare_recommendation_result")
    workflow.add_edge("compose_knowledge_answer", "prepare_knowledge_result")
    workflow.add_edge("prepare_recommendation_result", END)
    workflow.add_edge("prepare_knowledge_result", END)
    workflow.add_edge("no_match_response", END)
    workflow.add_edge("incomplete_product_info_response", END)
    return workflow.compile()


def build_cart_draft_graph(service: SkinAssistantService):
    """构建确认后的执行图；入口是前端提交的确认令牌。"""

    workflow = StateGraph(CartDraftState)

    def verify_confirmation(state: CartDraftState) -> dict[str, Bundle]:
        # 不接受前端上传的套餐内容，只根据一次性令牌取回服务端暂存的套餐。
        bundle = service.pending_bundle(state["confirmation_token"])
        policy = decide("create_cart_draft", confirmed=state["confirmed"])
        if policy.decision != "allow":
            service.audit("cart", "create_cart_draft", policy, bundle.bundle_id, "未创建草稿")
            raise PermissionError(policy.reason)
        return {"bundle": bundle}

    def revalidate_stock(state: CartDraftState) -> dict[str, list[dict[str, Any]]]:
        # 用户确认到真正写入之间可能发生库存变化，因此必须重新查询。
        return {
            "available_products": service._tool(
                "get_realtime_price_stock",
                {"sku_ids": [item.sku_id for item in state["bundle"].items]},
                "cart",
            )
        }

    def create_draft(state: CartDraftState) -> dict[str, str]:
        # 二次校验通过后才消耗令牌，失败时保留令牌以便业务方决定如何处理。
        bundle = state["bundle"]
        if not service.bundle_matches_revalidated_products(bundle, state["available_products"]):
            policy = decide("create_cart_draft", confirmed=True)
            service.audit("cart", "create_cart_draft", policy, bundle.bundle_id, "二次校验失败")
            raise ValueError("套餐中存在已下架、库存不足或价格变化商品")
        return {
            "cart_draft_id": service.persist_cart_draft(state["confirmation_token"], bundle)
        }

    workflow.add_node("verify_confirmation", verify_confirmation)
    workflow.add_node("revalidate_stock", revalidate_stock)
    workflow.add_node("create_cart_draft", create_draft)
    workflow.add_edge(START, "verify_confirmation")
    workflow.add_edge("verify_confirmation", "revalidate_stock")
    workflow.add_edge("revalidate_stock", "create_cart_draft")
    workflow.add_edge("create_cart_draft", END)
    return workflow.compile()
