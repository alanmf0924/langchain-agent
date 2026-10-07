import asyncio

from app.service import SkinAssistantService


def test_recommendation_is_only_in_stock_and_on_sale() -> None:
    # 回归边界：下架、缺货 SKU 绝不能混入固定推荐套餐。
    service = SkinAssistantService()
    result = service.build_result("换季干燥紧绷，预算 600 元", "run_test")
    assert result.query_plan.intent == "recommendation"
    assert result.requires_confirmation is False
    assert result.actions == []
    assert [item.sku_id for item in result.bundles[0].items] == [
        "sku_cleanser_100",
        "sku_serum_30",
        "sku_cream_50",
    ]
    assert all(item.version for item in result.evidence)


def test_cart_draft_stops_without_confirmation() -> None:
    # 回归边界：确认令牌存在也不能替代用户的 confirmed=True。
    service = SkinAssistantService(enable_cart_drafts=True)
    token = service.build_result("脸颊干，洗脸后紧绷", "run_test").actions[0]["confirmation_token"]
    try:
        service.create_cart_draft(token, confirmed=False)
    except PermissionError:
        pass
    else:
        raise AssertionError("未确认时不应创建购物车草稿")
    assert service.create_cart_draft(token, confirmed=True)[0].startswith("cart_draft_")


def test_cart_draft_stops_when_revalidated_price_changes() -> None:
    """金额按分比较；确认前价格变化时，旧套餐绝不能继续创建草稿。"""
    service = SkinAssistantService(enable_cart_drafts=True)
    service.answer_chain._chain = None
    token = service.build_result("脸颊干，洗脸后紧绷", "run_price_changed").actions[0]["confirmation_token"]
    service.catalog.products["sku_serum_30"].price_fen += 100

    try:
        service.create_cart_draft(token, confirmed=True)
    except ValueError as error:
        assert "价格变化" in str(error)
    else:
        raise AssertionError("价格变化后不应创建购物车草稿")


def test_safety_fallback_has_no_bundle() -> None:
    # 回归边界：安全词命中后不能生成套餐或确认动作。
    result = SkinAssistantService().build_result("脸上破溃且持续不适", "run_test")
    assert result.bundles == []
    assert result.query_plan.intent == "safety"
    assert result.requires_confirmation is False


def test_langgraph_exports_the_real_branches() -> None:
    # 防止文档和实际图分叉：导出的 Mermaid 必须包含意图、安全与检索节点。
    diagrams = SkinAssistantService().workflow_mermaid()
    assert "analyze_intent" in diagrams["recommendation"]
    assert "safety_response" in diagrams["recommendation"]
    assert "clarify_response" in diagrams["recommendation"]
    assert "prepare_recommendation_result" in diagrams["recommendation"]
    assert "verify_confirmation" in diagrams["cart_draft"]
    assert "revalidate_stock" in diagrams["cart_draft"]


def test_langgraph_server_entry_accepts_question_without_run_id() -> None:
    # Studio 只提交 question，initialize_run 必须补齐内部追踪 ID。
    from app.langgraph_entry import graph

    result = graph.invoke({"question": "脸上破溃且持续不适"})
    assert result["run_id"].startswith("run_")
    assert result["result"].bundles == []


def test_vague_question_requests_clarification_without_retrieval() -> None:
    result = SkinAssistantService().build_result("买什么", "run_test")
    assert result.query_plan.intent == "clarify"
    assert result.query_plan.sources == []
    assert result.evidence == []
    assert result.bundles == []


def test_order_question_is_refused_without_recommendation_retrieval_or_model_call() -> None:
    """订单属于商城服务，不可因助手的护肤词表误入商品推荐。"""
    service = SkinAssistantService()
    result = service.build_result("我的订单怎么查询？", "run_order_out_of_scope")
    trace = service.get_trace("run_order_out_of_scope")

    assert result.query_plan.intent == "out_of_scope"
    assert result.query_plan.next_action == "stop"
    assert result.query_plan.sources == []
    assert result.model_usage.model_call_count == 0
    assert result.model_usage.skipped_or_fallback_reason == "out_of_scope"
    assert result.evidence == []
    assert result.bundles == []
    assert result.actions == []
    assert "订单中心" in result.answer
    assert trace is not None
    assert all(event.stage != "tool" for event in trace.events)


def test_unrelated_question_is_refused_instead_of_requesting_skincare_details() -> None:
    result = SkinAssistantService().build_result("今天天气怎么样？", "run_unrelated_out_of_scope")

    assert result.query_plan.intent == "out_of_scope"
    assert "只能协助护肤选购" in result.answer
    assert "订单中心" not in result.answer
    assert "补充你的主要肤感" not in result.answer


def test_order_sse_stream_has_no_retrieval_or_recommendation_events() -> None:
    """前端应直接收到范围说明，不能先显示检索商品或确认购买的错误状态。"""
    service = SkinAssistantService()

    async def collect_events():
        return [event async for event in service.run_events("我的订单怎么查询？")]

    events = asyncio.run(collect_events())
    event_types = [event.type for event in events]
    plan = next(event.payload for event in events if event.type == "retrieval_plan")
    completed = next(event.payload for event in events if event.type == "run_completed")

    assert event_types == ["run_started", "retrieval_plan", "evidence_ready", "message_delta", "run_completed"]
    assert plan == {
        "intent": "out_of_scope",
        "sub_questions": [],
        "sources": [],
        "next_action": "stop",
    }
    assert completed["result"]["bundles"] == []
    assert completed["result"]["actions"] == []


def test_product_knowledge_uses_evidence_without_creating_bundle() -> None:
    result = SkinAssistantService().build_result("澄肌屏障修护精华怎么用？", "run_test")
    assert result.query_plan.intent == "product_knowledge"
    assert result.query_plan.sources == ["search_catalog", "retrieve_evidence"]
    assert result.evidence
    assert result.bundles == []


def test_named_out_of_stock_product_does_not_fall_back_to_unrelated_products() -> None:
    """点名缺货商品时，不能为了凑推荐混入别的 SKU 或资料。"""
    result = SkinAssistantService().build_result("澄肌舒缓精华怎么用？", "run_out_of_stock")
    assert result.query_plan.intent == "product_knowledge"
    assert result.evidence == []
    assert result.bundles == []
    assert result.model_usage.skipped_or_fallback_reason == "no_grounded_match"


def test_brightening_question_returns_bright_bundle() -> None:
    result = SkinAssistantService().build_result("想要焕亮美白产品", "run_test")
    assert result.query_plan.intent == "recommendation"
    assert result.bundles[0].bundle_id == "bundle_bright_7d"


def test_oily_skin_question_prefers_balance_bundle() -> None:
    result = SkinAssistantService().build_result("T 区油光明显，毛孔粗大，预算 600 元", "run_test")
    assert result.bundles[0].bundle_id == "bundle_balance_7d"
    assert [item.sku_id for item in result.bundles[0].items] == [
        "sku_oil_cleanser_120",
        "sku_oil_serum_30",
        "sku_oil_lotion_50",
    ]


def test_budget_shortfall_returns_a_disclosed_single_product_fallback() -> None:
    """预算不足以覆盖完整套餐时，不能静默超支，也不能直接结束推荐。"""
    service = SkinAssistantService()
    service.answer_chain._chain = None

    result = service.build_result("换季干燥紧绷，预算 300 元", "run_budget_fallback")

    assert result.bundles[0].bundle_id == "budget_single_sku_serum_30"
    assert result.bundles[0].bundle_price_fen == result.bundles[0].items[0].price_fen
    assert result.bundles[0].bundle_price_fen <= 30_000
    assert [item.sku_id for item in result.bundles[0].items] == ["sku_serum_30"]
    assert "300 元预算" in result.answer
    assert "不是完整护理套餐" in result.answer
    assert [item.sku_id for item in result.evidence] == ["sku_serum_30"]


def test_missing_ingredient_data_stops_an_avoidance_recommendation() -> None:
    """无法核验避开项时，未知不是“不含”，因此不能拿近似商品兜底。"""
    service = SkinAssistantService()

    result = service.build_result("换季干燥紧绷，预算 300 元，想避开香精", "run_data_gap")

    assert result.bundles == []
    assert result.evidence == []
    assert result.model_usage.skipped_or_fallback_reason == "incomplete_product_information"
    assert "无法确认是否避开香精" in result.answer


def test_verified_ingredient_disclosure_allows_an_avoidance_fallback() -> None:
    """仅在商城确认成分完整披露后，才可把未命中避开项视为可继续筛选。"""
    service = SkinAssistantService()
    service.answer_chain._chain = None
    for product in service.catalog.products.values():
        product.ingredient_disclosure_complete = True

    result = service.build_result("换季干燥紧绷，预算 300 元，想避开香精", "run_verified_avoidance")

    assert result.bundles[0].bundle_id == "budget_single_sku_serum_30"
    assert result.model_usage.skipped_or_fallback_reason == "model_not_configured"


def test_model_markdown_is_normalized_before_display() -> None:
    from app.answer_chain import GroundedAnswerChain

    text = GroundedAnswerChain.plain_text("## 建议\n- 使用 **精华**，参考 [资料](https://example.com)。")
    assert text == "建议\n使用 精华，参考 资料。"


def test_sse_exposes_auditable_plan_without_default_trade_action() -> None:
    service = SkinAssistantService()
    service.answer_chain._chain = None
    async def collect_events():
        return [event async for event in service.run_events("换季干燥紧绷，预算 600 元")]

    events = asyncio.run(collect_events())
    event_types = [item.type for item in events]
    plan = next(item.payload for item in events if item.type == "retrieval_plan")
    assert plan["intent"] == "recommendation"
    assert plan["sources"] == ["search_catalog", "get_realtime_price_stock", "retrieve_evidence"]
    assert "approval_required" not in event_types
