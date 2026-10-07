import asyncio
from time import sleep

from app.answer_chain import MAX_EVIDENCE_CHARS_PER_ITEM, GroundedAnswerChain
from app.persistence import SqlAssistantRepository
from app.service import SkinAssistantService


class FakeMessage:
    def __init__(self) -> None:
        self.content = "这是基于证据的回答。"
        self.usage_metadata = {"input_tokens": 120, "output_tokens": 45, "total_tokens": 165}
        self.response_metadata = {"model_name": "fake-model"}


class FakeChain:
    def invoke(self, _: dict) -> FakeMessage:
        return FakeMessage()


class TimeoutChain:
    """模拟 Provider 超时，确保不会由 SDK 或业务层额外重试。"""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, _: dict) -> FakeMessage:
        self.calls += 1
        raise TimeoutError("provider request timed out")


class MustNotRunChain:
    def invoke(self, _: dict) -> FakeMessage:
        raise AssertionError("模型预算为零时不应调用 Provider")


def test_evidence_context_is_hard_capped_before_model_call() -> None:
    evidence = [
        {"title": f"资料{i}", "version": "v1", "quote": "x" * 500}
        for i in range(4)
    ]
    text, count, _ = GroundedAnswerChain._evidence_text(evidence)
    assert count == 3
    assert text.count("资料") == 3
    assert "x" * (MAX_EVIDENCE_CHARS_PER_ITEM + 1) not in text


def test_provider_usage_is_preserved_when_provider_returns_it() -> None:
    service = SkinAssistantService(
        repository=SqlAssistantRepository(url="sqlite+pysqlite:///:memory:")
    )
    service.answer_chain._chain = FakeChain()
    result = service.build_result("换季干燥紧绷", "run_usage")
    assert result.answer == "这是基于证据的回答。"
    assert result.model_usage.provider_attempted is True
    assert result.model_usage.usage_reported is True
    assert result.model_usage.total_tokens == 165
    assert result.model_usage.output_token_limit == 300
    assert result.model_usage.model_call_count == 1
    assert result.model_usage.model_call_budget == 1
    assert result.model_usage.evidence_count == 3
    assert service.model_usage_logs[0].total_tokens == 165


def test_zero_model_call_branches_are_logged_without_fabricated_usage() -> None:
    # 此断言只关心本次服务的两条记录，不能继承其他测试的持久化审计。
    service = SkinAssistantService(
        repository=SqlAssistantRepository(url="sqlite+pysqlite:///:memory:")
    )
    service.answer_chain._chain = None

    recommendation = service.build_result("换季干燥紧绷，预算 600 元", "run_recommendation")
    safety = service.build_result("脸上破溃且持续不适", "run_safety")

    assert recommendation.model_usage.provider_attempted is False
    assert recommendation.model_usage.skipped_or_fallback_reason == "model_not_configured"
    assert safety.model_usage.provider_attempted is False
    assert safety.model_usage.skipped_or_fallback_reason == "safety_policy"
    assert [item.run_id for item in service.model_usage_logs] == ["run_recommendation", "run_safety"]
    assert service.model_usage_logs[0].total_tokens is None


def test_model_timeout_degrades_after_one_attempt_without_retry() -> None:
    service = SkinAssistantService()
    provider = TimeoutChain()
    service.answer_chain._chain = provider

    result = service.build_result("换季干燥紧绷，预算 600 元", "run_model_timeout")

    assert result.model_usage.provider_attempted is True
    assert result.model_usage.model_call_count == 1
    assert result.model_usage.model_call_budget == 1
    assert result.model_usage.skipped_or_fallback_reason == "model_timeout"
    assert provider.calls == 1
    assert result.bundles


def test_zero_model_budget_uses_grounded_fallback_without_provider_call() -> None:
    service = SkinAssistantService(max_model_calls_per_run=0)
    service.answer_chain._chain = MustNotRunChain()

    result = service.build_result("换季干燥紧绷，预算 600 元", "run_model_budget_zero")

    assert result.model_usage.provider_attempted is False
    assert result.model_usage.model_call_count == 0
    assert result.model_usage.model_call_budget == 0
    assert result.model_usage.skipped_or_fallback_reason == "model_call_budget_exhausted"
    assert result.bundles


def test_sse_run_timeout_stops_stream_without_creating_a_trade_action() -> None:
    service = SkinAssistantService()
    service.run_timeout_seconds = 0.01

    def slow_result(*_: str):
        sleep(0.05)
        raise AssertionError("超时后不应将迟到结果写为成功 SSE")

    service.build_result = slow_result  # type: ignore[method-assign]

    async def collect_events():
        return [event async for event in service.run_events("换季干燥紧绷")]

    events = asyncio.run(collect_events())
    assert events[-1].type == "run_failed"
    assert events[-1].payload["fallback_reason"] == "agent_run_timeout"
