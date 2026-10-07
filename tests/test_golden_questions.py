import json
from pathlib import Path

from app.service import SkinAssistantService


def test_golden_questions_follow_intent_and_evidence_contract() -> None:
    """黄金集覆盖长短、清晰度、复杂度和噪声，不用公开网页充当业务事实来源。"""
    cases = json.loads((Path(__file__).parents[1] / "eval" / "golden_questions.json").read_text())
    service = SkinAssistantService()
    # 黄金集检验路由和证据契约，不向外部模型服务发起 30 次网络请求。
    service.answer_chain._chain = None

    for case in cases:
        result = service.build_result(case["prompt"], case["id"])
        assert result.query_plan.intent == case["expected_intent"], case["id"]
        assert result.query_plan.next_action == case["expected_action"], case["id"]
        assert bool(result.evidence) is case["must_have_evidence"], case["id"]
        if case["expected_action"] != "answer":
            assert result.bundles == [], case["id"]
            assert result.actions == [], case["id"]
