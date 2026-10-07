from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv

from app.answer_chain import GeneratedAnswer, GroundedAnswerChain
from app.catalog import Catalog
from app.embeddings import EmbeddingProvider, build_embedding_provider
from app.knowledge_base import KnowledgeBase
from app.mock_data import BUNDLE_RECIPES, DOCUMENTS, BundleRecipe
from app.models import (
    AgentEvent,
    AgentResult,
    AgentTrace,
    AuditLog,
    Bundle,
    BundleItem,
    ModelUsage,
    ModelUsageLog,
    Product,
    QueryPlan,
    TraceEvent,
    TraceStage,
    TraceStatus,
)
from app.persistence import SqlAssistantRepository
from app.policy import PolicyDecision, decide
from app.query_planner import COMMERCE_SERVICE_WORDS, plan_question
from app.tools import build_read_tools
from app.workflows import build_cart_draft_graph, build_recommendation_graph

# 在构造 Catalog 和 Repository 前读取本地运行配置。否则 Catalog 会在 .env 的
# CATALOG_SOURCE=database 尚未生效时错误回退到 mock，造成真实数据库配置形同未启用。
# 使用 override=False，容器或部署平台显式注入的配置始终优先。
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
# 本地 SiliconFlow 凭据与默认 .env 分离；环境变量优先，避免覆盖部署配置。
load_dotenv(Path(__file__).resolve().parents[1] / ".siliconflow.env", override=False)

# 套餐来自经过审核的业务配置，模型不能根据自然语言自行拼接 SKU。
EXPECTED_SKUS = list(BUNDLE_RECIPES[0].sku_ids)
NEAR_BUDGET_OVERAGE_RATE = 0.15
AVOIDANCE_TERMS = ("香精", "酒精", "精油")


class SkinAssistantService:
    """编排层：LangChain Tool/Chain 与不可绕过的交易策略分离。"""

    expected_skus = EXPECTED_SKUS

    def __init__(
        self,
        enable_cart_drafts: bool | None = None,
        embedder: EmbeddingProvider | None = None,
        repository: SqlAssistantRepository | None = None,
        confirmation_ttl_seconds: int | None = None,
        max_model_calls_per_run: int | None = None,
        graph_recursion_limit: int | None = None,
        run_timeout_seconds: int | None = None,
    ) -> None:
        self.catalog = Catalog()
        # 评估或离线任务可显式传入本地 Embedder，避免读取运行环境中的远程配置。
        self.knowledge_base = KnowledgeBase(
            DOCUMENTS, embedder=embedder if embedder is not None else build_embedding_provider()
        )
        self.tools = {item.name: item for item in build_read_tools(self.catalog, self.knowledge_base)}
        self.answer_chain = GroundedAnswerChain()
        # V1 只验证问答价值，交易草稿必须显式开启才会暴露给前端。
        self.enable_cart_drafts = (
            enable_cart_drafts
            if enable_cart_drafts is not None
            else os.getenv("ENABLE_CART_DRAFTS", "false").lower() == "true"
        )
        # 服务默认连接 DATABASE_URL；生产环境配置校验禁止把 SQLite 当成业务持久化库。
        self.repository = repository or SqlAssistantRepository()
        configured_ttl = confirmation_ttl_seconds or int(
            os.getenv("CONFIRMATION_TOKEN_TTL_SECONDS", "900")
        )
        if not 60 <= configured_ttl <= 86_400:
            raise ValueError("CONFIRMATION_TOKEN_TTL_SECONDS 必须在 60 到 86400 秒之间")
        self.confirmation_ttl_seconds = configured_ttl
        self.max_model_calls_per_run = self._configured_int(
            "MODEL_MAX_CALLS_PER_RUN", max_model_calls_per_run, default=1, minimum=0, maximum=3
        )
        self.graph_recursion_limit = self._configured_int(
            "LANGGRAPH_RECURSION_LIMIT", graph_recursion_limit, default=24, minimum=8, maximum=64
        )
        self.run_timeout_seconds = self._configured_int(
            "AGENT_RUN_TIMEOUT_SECONDS", run_timeout_seconds, default=12, minimum=5, maximum=60
        )
        self.recommendation_graph = build_recommendation_graph(self)
        self.cart_draft_graph = build_cart_draft_graph(self)

    @staticmethod
    def _configured_int(
        env_name: str, supplied: int | None, default: int, minimum: int, maximum: int
    ) -> int:
        """所有循环/超时配置都限幅，避免环境变量意外打开无限调用或无限等待。"""
        try:
            value = supplied if supplied is not None else int(os.getenv(env_name, str(default)))
        except ValueError as error:
            raise ValueError(f"{env_name} 必须是整数") from error
        if not minimum <= value <= maximum:
            raise ValueError(f"{env_name} 必须在 {minimum} 到 {maximum} 之间")
        return value

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)

    def audit(
        self, run_id: str, action: str, policy: PolicyDecision, input_summary: str, outcome: str
    ) -> None:
        """追加不可省略的审计记录；调用方只传摘要，避免把敏感输入写入日志。"""
        self.repository.add_audit_log(
            AuditLog(
                at=self.now(),
                run_id=run_id,
                action=action,
                decision=policy.decision,
                reason=policy.reason,
                input_summary=input_summary,
                outcome=outcome,
            )
        )

    @property
    def audit_logs(self) -> list[AuditLog]:
        """兼容既有 API；实际来源是可配置的持久化 Repository。"""
        return self.repository.list_audit_logs()

    def record_model_usage(self, run_id: str, usage: ModelUsage) -> None:
        """记录调用与零调用分支；缺失的服务商 token usage 保持 null，不作估算。"""
        self.repository.add_model_usage_log(
            ModelUsageLog(at=self.now(), run_id=run_id, **usage.model_dump())
        )
        self.trace_event(
            run_id,
            "generation",
            "skipped" if not usage.provider_attempted else "completed",
            "已记录回答生成或确定性降级状态",
            {
                "provider_attempted": usage.provider_attempted,
                "model": usage.model,
                "fallback_reason": usage.skipped_or_fallback_reason,
                "model_call_count": usage.model_call_count,
                "model_call_budget": usage.model_call_budget,
                "evidence_count": usage.evidence_count,
            },
        )

    @property
    def model_usage_logs(self) -> list[ModelUsageLog]:
        """只暴露脱敏用量字段；Prompt、用户原文和密钥均不进入存储。"""
        return self.repository.list_model_usage_logs()

    def start_trace(self, run_id: str, question: str) -> None:
        """只保存问题哈希，既支持同输入复核，也不在运维接口重复暴露用户原文。"""
        trace = AgentTrace(
            run_id=run_id,
            question_sha256=hashlib.sha256(question.encode("utf-8")).hexdigest(),
            question_length=len(question),
            started_at=self.now(),
        )
        self.repository.save_trace(trace)

    def trace_event(
        self,
        run_id: str,
        stage: TraceStage,
        status: TraceStatus,
        summary: str,
        details: dict[str, Any],
    ) -> None:
        """仅追加可复核的结构化事实；不保存模型思维链和未脱敏工具输入。"""
        trace = self.repository.get_trace(run_id)
        if trace is None:
            return
        trace.events.append(
            TraceEvent(
                at=self.now(),
                stage=stage,
                status=status,
                summary=summary,
                details=details,
            )
        )
        self.repository.save_trace(trace)

    def finish_trace(self, run_id: str) -> None:
        trace = self.repository.get_trace(run_id)
        if trace is not None:
            trace.finished_at = self.now()
            self.repository.save_trace(trace)

    def get_trace(self, run_id: str) -> AgentTrace | None:
        """供 API 和运维界面按 run_id 查找完整公开轨迹。"""
        return self.repository.get_trace(run_id)

    def available_products(self) -> list[Product]:
        """商品目录展示过滤；最终推荐还会增加库存过滤。"""
        return self.catalog.list_visible()

    def visible_product(self, sku_id: str) -> Product | None:
        """完整商品页的实时可见性检查；上架状态改变后旧链接不能继续展示。"""
        return self.catalog.get_visible_by_sku(sku_id)

    def compose_answer(
        self, question: str, evidence: list[dict[str, Any]], completed_model_calls: int
    ) -> GeneratedAnswer:
        """模型调用只能在图节点内消耗显式预算；预算耗尽时仍保留有依据的确定性回答。"""
        generated = self.answer_chain.answer(
            question,
            evidence,
            allow_model_call=completed_model_calls < self.max_model_calls_per_run,
        )
        usage = generated.usage.model_copy(
            update={
                "model_call_count": completed_model_calls + generated.usage.model_call_count,
                "model_call_budget": self.max_model_calls_per_run,
            }
        )
        return GeneratedAnswer(text=generated.text, usage=usage)

    @staticmethod
    def is_safety_case(question: str) -> bool:
        """高风险词命中时跳过模型和商品推荐，优先返回线下处理建议。"""
        return plan_question(question).intent == "safety"

    @staticmethod
    def build_query_plan(question: str) -> QueryPlan:
        """统一入口先生成意图与检索计划，后续节点只执行该计划允许的路径。"""
        return plan_question(question)

    @staticmethod
    def safety_result(query_plan: QueryPlan) -> AgentResult:
        """安全分支的固定结果，确保模型故障或提示词变化不会削弱兜底。"""
        return AgentResult(
            answer="你描述了可能需要优先线下处理的不适情况。此时不建议继续推荐产品，请停止使用可能引起不适的产品并线下咨询专业人士。",
            query_plan=query_plan,
            model_usage=ModelUsage(skipped_or_fallback_reason="safety_policy"),
            evidence=[],
            bundles=[],
            actions=[],
            requires_confirmation=False,
        )

    @staticmethod
    def clarify_result(query_plan: QueryPlan) -> AgentResult:
        """信息不足或不在首版范围时，不用猜测替代追问。"""
        return AgentResult(
            answer=(
                "为了给出有依据的建议，请补充你的主要肤感或诉求（如干燥、紧绷、泛红）、"
                "预算，以及正在使用或希望避开的产品。"
            ),
            query_plan=query_plan,
            model_usage=ModelUsage(skipped_or_fallback_reason="need_clarification"),
            evidence=[],
            bundles=[],
            actions=[],
            requires_confirmation=False,
        )

    @staticmethod
    def out_of_scope_result(query_plan: QueryPlan, question: str) -> AgentResult:
        """范围外问题只给出贴合问题的能力边界，不尝试检索、生成推荐或交易动作。"""
        normalized = question.lower()
        is_commerce_service_question = any(
            word in normalized for word in COMMERCE_SERVICE_WORDS
        )
        answer = (
            "抱歉，我不能查询或处理订单、物流、支付及售后问题。请前往订单中心或联系商城客服；"
            "我可以继续协助护肤选购、商品成分和用法相关的问题。"
            if is_commerce_service_question
            else "抱歉，我只能协助护肤选购、商品成分和用法相关的问题。请换一个护肤相关的问题。"
        )
        return AgentResult(
            answer=answer,
            query_plan=query_plan,
            model_usage=ModelUsage(skipped_or_fallback_reason="out_of_scope"),
            evidence=[],
            bundles=[],
            actions=[],
            requires_confirmation=False,
        )

    @staticmethod
    def handoff_result(query_plan: QueryPlan) -> AgentResult:
        """多次澄清仍不能识别目的时的确定性收口，禁止继续调用模型猜测。"""
        return AgentResult(
            answer=(
                "这段咨询暂时还无法确认你的主要选购目的。为避免继续猜测造成不合适的建议，"
                "我已保留本段对话并提交人工导购待接入；你也可以开始一段新的咨询。"
            ),
            query_plan=query_plan,
            model_usage=ModelUsage(skipped_or_fallback_reason="handoff_after_unresolved_context"),
            evidence=[],
            bundles=[],
            actions=[],
            requires_confirmation=False,
            requires_handoff=True,
        )

    @staticmethod
    def no_match_result(query_plan: QueryPlan, question: str) -> AgentResult:
        """没有可验证的近似方案时明确收口，不把超预算商品伪装成匹配结果。"""
        budget_fen = SkinAssistantService._budget_fen(question)
        budget_hint = (
            f"；在 {budget_fen // 100} 元预算附近也没有价格足够接近的可验证单品"
            if budget_fen is not None
            else ""
        )
        return AgentResult(
            answer=(
                "当前没有找到可售且具备审核资料的匹配商品"
                f"{budget_hint}，因此暂不推荐。你可以调整预算、补充可接受的替代范围，或转人工导购。"
            ),
            query_plan=query_plan,
            model_usage=ModelUsage(skipped_or_fallback_reason="no_grounded_match"),
            evidence=[],
            bundles=[],
            actions=[],
            requires_confirmation=False,
        )

    @staticmethod
    def _requested_avoidances(question: str) -> list[str]:
        """只识别当前商品资料可核验的避开项，避免把模糊偏好误作成分事实。"""
        normalized = question.lower()
        return [
            term
            for term in AVOIDANCE_TERMS
            if term in normalized and any(marker in normalized for marker in ("避开", "不含", "不要", "不想"))
        ]

    @classmethod
    def filter_candidates_for_constraints(
        cls, question: str, candidates: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], bool]:
        """过滤已知不满足的成分，并把缺失资料作为可见的失败状态。"""
        avoidances = cls._requested_avoidances(question)
        if not avoidances:
            return candidates, True

        filtered: list[dict[str, Any]] = []
        has_unknown_ingredients = False
        for candidate in candidates:
            ingredients = candidate.get("ingredients")
            if (
                candidate.get("ingredient_disclosure_complete") is not True
                or not isinstance(ingredients, list)
                or not ingredients
            ):
                has_unknown_ingredients = True
                continue
            normalized_ingredients = " ".join(
                item.lower() for item in ingredients if isinstance(item, str)
            )
            if any(term in normalized_ingredients for term in avoidances):
                continue
            filtered.append(candidate)

        # 仍有资料完整的候选时可以继续；只有所有候选都因资料缺失而无法判断才阻断。
        return filtered, not (has_unknown_ingredients and not filtered)

    @classmethod
    def incomplete_product_info_result(cls, query_plan: QueryPlan, question: str) -> AgentResult:
        avoidances = "、".join(cls._requested_avoidances(question)) or "所避开的成分"
        return AgentResult(
            answer=(
                f"当前候选商品缺少可核验的成分资料，无法确认是否避开{avoidances}，"
                "因此不会把未知成分的商品作为替代推荐。请补充完整成分表后重试，或转人工导购核验。"
            ),
            query_plan=query_plan,
            model_usage=ModelUsage(skipped_or_fallback_reason="incomplete_product_information"),
            evidence=[],
            bundles=[],
            actions=[],
            requires_confirmation=False,
        )

    def _tool(self, name: str, payload: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
        """执行前再次检查策略，并记录工具结果；这是 Tool 层的最后一道授权门。"""
        policy = decide(name)
        if policy.decision != "allow":
            self.audit(run_id, name, policy, "工具输入已隐藏", "策略拒绝")
            self.trace_event(
                run_id, "tool", "blocked", f"{name} 被策略拒绝", {"tool": name, "reason": policy.reason}
            )
            return []
        # request_id 只用于商城侧关联与脱敏审计，不携带用户原文。
        result = self.tools[name].invoke({**payload, "request_id": run_id})
        self.audit(run_id, name, policy, "只读工具输入", f"返回 {len(result)} 条结果")
        details: dict[str, Any] = {"tool": name, "policy": policy.decision, "result_count": len(result)}
        if name == "retrieve_evidence":
            details["sources"] = [
                {
                    "id": item["id"],
                    "title": item["title"],
                    "version": item["version"],
                    "source_chunk_id": item.get("source_chunk_id"),
                }
                for item in result
            ]
        elif name in {"search_catalog", "get_realtime_price_stock"}:
            details["sku_ids"] = [item["sku_id"] for item in result]
        self.trace_event(run_id, "tool", "completed", f"{name} 返回 {len(result)} 条结果", details)
        return result

    @staticmethod
    def _budget_fen(question: str) -> int | None:
        """只从明确预算表达读取金额并转为分，避免浮点误差和“7 日”误判为预算。"""
        matched = re.search(r"(?:预算|不超过|以内|控制在)\s*(\d{2,5})\s*(?:元|块)?", question)
        return int(matched.group(1)) * 100 if matched else None

    @classmethod
    def _best_recipe(cls, question: str, products: list[dict[str, Any]]) -> BundleRecipe | None:
        available = {item["sku_id"]: item for item in products}
        budget_fen = cls._budget_fen(question)
        ranked: list[tuple[int, BundleRecipe]] = []
        for recipe in BUNDLE_RECIPES:
            if not all(sku in available for sku in recipe.sku_ids):
                continue
            # 预算是上限而不是仅用于排序的软偏好；完整套餐不能静默超支。
            if budget_fen is not None and recipe.bundle_price_fen > budget_fen:
                continue
            list_price_fen = sum(available[sku]["price_fen"] for sku in recipe.sku_ids)
            # 不同诉求通过命中信号获得不同权重；预算只影响排序，不能绕过库存与上架校验。
            score = sum(40 for signal in recipe.signals if signal in question)
            if budget_fen is not None:
                score += 24 if recipe.bundle_price_fen <= budget_fen else -24
                score += 8 if list_price_fen <= budget_fen else 0
            ranked.append((score, recipe))
        if not ranked:
            return None
        return min(ranked, key=lambda pair: (-pair[0], pair[1].bundle_price_fen, pair[1].bundle_id))[1]

    @classmethod
    def _best_budget_fallback_product(
        cls, question: str, products: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """完整套餐超预算时，选择一个价格接近预算的已审核、可售单品作为明确降级。"""
        budget_fen = cls._budget_fen(question)
        if budget_fen is None:
            return None
        nearby_limit_fen = round(budget_fen * (1 + NEAR_BUDGET_OVERAGE_RATE))
        eligible = [item for item in products if item["price_fen"] <= nearby_limit_fen]
        if not eligible:
            return None
        # Tool 已按诉求相关性排序；在同等可用前提下，优先与预算差距最小的单品。
        return min(
            enumerate(eligible), key=lambda pair: (abs(pair[1]["price_fen"] - budget_fen), pair[0])
        )[1]

    def _database_recommendation_product(
        self, question: str, products: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """数据库模式只展示真实 SKU，不能把新商品伪装成旧的审核套餐。

        固定套餐仍必须命中受审核的 BundleRecipe。数据库尚未配置套餐时，按已完成的
        检索排序展示一件真实在售单品；用户明确预算时沿用价格上限规则。
        """
        if not products:
            return None
        if self.catalog.database_catalog is None:
            return None
        budget_fen = self._budget_fen(question)
        if budget_fen is None:
            return products[0]
        return self._best_budget_fallback_product(question, products)

    def has_available_bundle(self, question: str, products: list[dict[str, Any]]) -> bool:
        return (
            self._best_recipe(question, products) is not None
            or self._best_budget_fallback_product(question, products) is not None
            or self._database_recommendation_product(question, products) is not None
        )

    @staticmethod
    def bundle_matches_revalidated_products(bundle: Bundle, products: list[dict[str, Any]]) -> bool:
        """确认阶段必须同时校验 SKU、可售性和金额分值，任何价格变化都使旧草稿失效。"""
        available_by_sku = {item["sku_id"]: item for item in products}
        return all(
            (current := available_by_sku.get(item.sku_id)) is not None
            and current["price_fen"] == item.price_fen
            for item in bundle.items
        )

    def _bundle(self, question: str, products: list[dict[str, Any]]) -> Bundle:
        """从可售候选中选择权重最高的审核套餐，避免固定 SKU 或部分缺货套餐进入结果。"""
        indexed = {item["sku_id"]: item for item in products}
        recipe = self._best_recipe(question, products)
        if recipe is None:
            item = self._database_recommendation_product(question, products)
            if item is not None:
                return Bundle(
                    bundle_id=f"database_single_{item['sku_id']}",
                    name="本次真实商品推荐",
                    bundle_price_fen=item["price_fen"],
                    list_price_fen=item["price_fen"],
                    items=[
                        BundleItem(
                            sku_id=item["sku_id"],
                            name=item["name"],
                            spec=item["spec"],
                            price_fen=item["price_fen"],
                            image_url=self._primary_image_url(item),
                        )
                    ],
                    routine=[item["usage"] or "请按商品说明使用"],
                    caution=item["cautions"] or "首次使用前请先阅读商品说明。",
                )
            item = self._best_budget_fallback_product(question, products)
            if item is None:
                raise ValueError("没有满足当前肤感、预算与库存条件的推荐套餐")
            return Bundle(
                bundle_id=f"budget_single_{item['sku_id']}",
                name="预算内单品替代建议",
                bundle_price_fen=item["price_fen"],
                list_price_fen=item["price_fen"],
                items=[
                    BundleItem(
                        sku_id=item["sku_id"],
                        name=item["name"],
                        spec=item["spec"],
                        price_fen=item["price_fen"],
                        image_url=self._primary_image_url(item),
                    )
                ],
                routine=["先从这一件开始使用，并按商品资料中的用法与注意事项执行"],
                caution=item["cautions"],
            )
        selected = [indexed[sku] for sku in recipe.sku_ids]
        return Bundle(
            bundle_id=recipe.bundle_id,
            name=recipe.name,
            bundle_price_fen=recipe.bundle_price_fen,
            list_price_fen=sum(item["price_fen"] for item in selected),
            items=[
                BundleItem(
                    sku_id=item["sku_id"],
                    name=item["name"],
                    spec=item["spec"],
                    price_fen=item["price_fen"],
                    image_url=self._primary_image_url(item),
                )
                for item in selected
            ],
            routine=list(recipe.routine),
            caution=recipe.caution,
        )

    @staticmethod
    def _primary_image_url(product: dict[str, Any]) -> str:
        """把后端快照的首张图带入 SSE；字段异常时安全降级为空图。"""
        image_urls = product.get("image_urls")
        if not isinstance(image_urls, list) or not image_urls:
            return ""
        primary = image_urls[0]
        return primary if isinstance(primary, str) else ""

    def prepare_recommendation_result(
        self,
        bundle: Bundle,
        answer: str,
        evidence: list[dict[str, Any]],
        run_id: str,
        query_plan: QueryPlan,
        model_usage: ModelUsage,
        question: str,
    ) -> AgentResult:
        """组装推荐结果；V1 默认不暴露交易动作，开启后仍须人工确认。"""
        answer = self._with_budget_fallback_explanation(answer, bundle, question)
        if not self.enable_cart_drafts:
            return AgentResult(
                answer=answer,
                query_plan=query_plan,
                model_usage=model_usage,
                evidence=evidence,
                bundles=[bundle],
                actions=[],
                requires_confirmation=False,
            )
        token = f"confirm_{uuid4().hex}"
        created_at = self.now()
        self.repository.create_confirmation(
            token=token,
            bundle=bundle,
            run_id=run_id,
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=self.confirmation_ttl_seconds),
        )
        policy = decide("create_cart_draft")
        self.audit(run_id, "create_cart_draft", policy, bundle.bundle_id, "等待用户确认")
        return AgentResult(
            answer=answer,
            query_plan=query_plan,
            model_usage=model_usage,
            evidence=evidence,
            bundles=[bundle],
            actions=[
                {
                    "tool": "create_cart_draft",
                    "bundle_id": bundle.bundle_id,
                    "confirmation_token": token,
                    "requires_confirmation": True,
                    "summary": "确认后才会创建购物车草稿；创建前会再次校验价格和库存。",
                }
            ],
            requires_confirmation=True,
        )

    @classmethod
    def _with_budget_fallback_explanation(cls, answer: str, bundle: Bundle, question: str) -> str:
        """由服务端公开降级原因，避免模型把单品建议表述成完整套餐。"""
        if not bundle.bundle_id.startswith("budget_single_"):
            return answer
        budget_fen = cls._budget_fen(question)
        budget_hint = f"{budget_fen // 100} 元" if budget_fen is not None else "当前"
        return (
            f"没有找到同时满足当前诉求与 {budget_hint}预算的完整套餐，"
            "已改为提供一件可售且有审核资料的单品替代。"
            "它不是完整护理套餐，请先结合商品卡中的当前价格、用法和注意事项决定是否尝试。\n\n"
            f"{answer}"
        )

    @staticmethod
    def prepare_knowledge_result(
        answer: str, evidence: list[dict[str, Any]], query_plan: QueryPlan, model_usage: ModelUsage
    ) -> AgentResult:
        """产品知识问题只返回证据回答，不附带套餐或交易动作。"""
        return AgentResult(
            answer=answer,
            query_plan=query_plan,
            model_usage=model_usage,
            evidence=evidence,
            bundles=[],
            actions=[],
            requires_confirmation=False,
        )

    def build_result(self, question: str, run_id: str) -> AgentResult:
        """FastAPI 与测试共用的 LangGraph 调用入口，业务步骤由图而非此方法串联。"""
        self.start_trace(run_id, question)
        preview_plan = self.build_query_plan(question)
        self.trace_event(
            run_id,
            "intent",
            "completed",
            f"识别为 {preview_plan.intent}，下一步 {preview_plan.next_action}",
            preview_plan.model_dump(mode="json"),
        )
        try:
            state = self.recommendation_graph.invoke(
                {"question": question, "run_id": run_id},
                {"recursion_limit": self.graph_recursion_limit},
            )
            result = state["result"]
            # 安全、追问和无匹配路径不经过 compose_answer，也必须暴露实际预算为零调用。
            result.model_usage = result.model_usage.model_copy(
                update={"model_call_budget": self.max_model_calls_per_run}
            )
            self.record_model_usage(run_id, result.model_usage)
            if result.query_plan.intent == "safety":
                self.trace_event(
                    run_id,
                    "safety",
                    "blocked",
                    "安全策略已停止商品推荐和交易动作",
                    {"fallback_reason": result.model_usage.skipped_or_fallback_reason},
                )
            elif result.query_plan.intent == "out_of_scope":
                self.trace_event(
                    run_id,
                    "safety",
                    "blocked",
                    "范围外问题已停止商品检索、推荐和交易动作",
                    {"fallback_reason": result.model_usage.skipped_or_fallback_reason},
                )
            self.trace_event(
                run_id,
                "evidence",
                "completed" if result.evidence else "skipped",
                f"最终公开 {len(result.evidence)} 条证据",
                {
                    "sources": [
                        {
                            "id": item.id,
                            "title": item.title,
                            "version": item.version,
                            "source_chunk_id": item.source_chunk_id,
                        }
                        for item in result.evidence
                    ]
                },
            )
            self.trace_event(
                run_id,
                "transaction",
                "blocked" if result.requires_confirmation else "skipped",
                "交易动作等待确认" if result.requires_confirmation else "本次没有执行交易动作",
                {"requires_confirmation": result.requires_confirmation, "actions": result.actions},
            )
            self.trace_event(
                run_id,
                "completed",
                "completed",
                "问答链路完成，可按阶段回查",
                {"intent": result.query_plan.intent, "next_action": result.query_plan.next_action},
            )
            return result
        except Exception:
            self.trace_event(run_id, "completed", "failed", "问答链路异常结束", {})
            raise
        finally:
            self.finish_trace(run_id)

    async def run_events(self, question: str) -> AsyncIterator[AgentEvent]:
        """逐条产出公开 SSE 事件，让浏览器先获得反馈再等待检索与生成完成。"""
        run_id = f"run_{uuid4().hex}"
        event = lambda event_type, payload: AgentEvent(
            type=event_type, run_id=run_id, at=self.now(), payload=payload
        )
        # 调用图前先发出已知的公开状态，避免用户面对无反馈的长时间等待。
        preview_plan = self.build_query_plan(question)
        if preview_plan.intent == "out_of_scope":
            yield event("run_started", {"summary": "已识别为非护肤选购问题，将直接说明服务范围"})
        else:
            yield event("run_started", {"summary": "已接收肤况、预算与选购偏好"})
            yield event(
                "message_delta",
                {"text": "收到你的需求，我正在整理肤感、预算并检索已审核资料。", "summary": "已开始整理需求"},
            )
        yield event(
            "retrieval_plan",
            {
                "intent": preview_plan.intent,
                "sub_questions": preview_plan.sub_questions,
                "sources": preview_plan.sources,
                "next_action": preview_plan.next_action,
            },
        )
        for tool_name in preview_plan.sources:
            yield event("tool_started", {"tool": tool_name, "summary": f"正在执行 {tool_name}"})

        try:
            # 图调用会触发检索和可能的模型网络请求，放到线程中才能让已发出的 SSE 首帧立即刷新到浏览器。
            result = await asyncio.wait_for(
                asyncio.to_thread(self.build_result, question, run_id),
                timeout=self.run_timeout_seconds,
            )
        except TimeoutError:
            yield event(
                "run_failed",
                {
                    "summary": "本次核验超时，未继续调用模型或创建草稿。请稍后重试。",
                    "fallback_reason": "agent_run_timeout",
                },
            )
            return
        except Exception:  # noqa: BLE001 - 外部模型或检索故障不能让 SSE 无反馈地中断。
            yield event("run_failed", {"summary": "检索或生成暂时不可用，请稍后再试。"})
            return

        for tool_name in result.query_plan.sources:
            yield event("tool_finished", {"tool": tool_name, "summary": f"已完成 {tool_name}"})
        yield event(
            "evidence_ready",
            {
                "evidence": [item.model_dump() for item in result.evidence],
                "summary": "已准备已审核资料证据",
            },
        )
        yield event(
            "message_delta",
            {"text": result.answer, "summary": "正在以打字机效果呈现选购说明"},
        )
        if result.requires_confirmation:
            yield event(
                "approval_required", {"actions": result.actions, "summary": "确认后才会创建购物车草稿"}
            )
        yield event(
            "run_completed",
            {"result": result.model_dump(mode="json"), "summary": "本次问答已完成"},
        )

    def create_cart_draft(self, token: str, confirmed: bool) -> tuple[str, Bundle]:
        """进入第二张 LangGraph：先验确认，再校库存，最后才执行草稿写入。"""
        state = self.cart_draft_graph.invoke(
            {"confirmation_token": token, "confirmed": confirmed},
            {"recursion_limit": self.graph_recursion_limit},
        )
        return state["cart_draft_id"], state["bundle"]

    def pending_bundle(self, token: str) -> Bundle:
        """确认前只读取服务端令牌快照，并明确区分过期、已用和不存在。"""
        confirmation = self.repository.get_pending_confirmation(token, self.now())
        if confirmation is None:
            raise KeyError("确认令牌不存在")
        if confirmation.state == "expired":
            raise KeyError("确认令牌已过期，请重新发起推荐")
        if confirmation.state != "pending":
            raise KeyError("确认令牌已使用，请勿重复提交")
        return confirmation.bundle

    def persist_cart_draft(self, token: str, bundle: Bundle) -> str:
        """草稿持久化发生在二次库存校验后；成功回执不等于商城订单或支付成功。"""
        cart_draft_id = self.new_cart_draft_id()
        now = self.now()
        decision = decide("create_cart_draft", confirmed=True)
        if not self.repository.consume_confirmation_and_create_draft(
            token,
            bundle,
            cart_draft_id,
            now,
            AuditLog(
                at=now,
                run_id="cart",
                action="create_cart_draft",
                decision=decision.decision,
                reason=decision.reason,
                input_summary=bundle.bundle_id,
                outcome="购物车草稿已创建",
            ),
        ):
            raise KeyError("确认令牌无效、已过期或已使用")
        return cart_draft_id

    @staticmethod
    def new_cart_draft_id() -> str:
        """Mock 草稿标识；生产环境应由购物车服务或数据库生成。"""
        return f"cart_draft_{uuid4().hex[:12]}"

    def workflow_mermaid(self) -> dict[str, str]:
        """返回由已编译 LangGraph 生成的图定义，供前端或文档直接渲染。"""
        return {
            "recommendation": self.recommendation_graph.get_graph().draw_mermaid(),
            "cart_draft": self.cart_draft_graph.get_graph().draw_mermaid(),
        }
