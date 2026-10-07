from __future__ import annotations

import json
import sys
from types import ModuleType

from app.models import AgentResult, ModelUsage, QueryPlan
from app.observability import AgentObservability


def test_observability_is_a_safe_noop_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    observer = AgentObservability().start_run("request-1", "换季干燥紧绷")
    observer.record_result(
        "run-1",
        AgentResult(
            answer="不应被发送到观测系统",
            query_plan=QueryPlan(intent="clarify", sub_questions=[], sources=[], next_action="clarify"),
            model_usage=ModelUsage(skipped_or_fallback_reason="need_clarification"),
            evidence=[],
            bundles=[],
            actions=[],
            requires_confirmation=False,
        ),
        "completed",
    )
    observer.record_failure("run-1", "test")
    observer.close()


def test_observability_sends_hashes_and_structured_summary_only(monkeypatch) -> None:
    class FakeSpan:
        def __init__(self) -> None:
            self.updates: list[dict[str, object]] = []

        def update(self, **kwargs: object) -> None:
            self.updates.append(kwargs)

    class FakeContext:
        def __init__(self, span: FakeSpan) -> None:
            self.span = span
            self.closed = False

        def __enter__(self) -> FakeSpan:
            return self.span

        def __exit__(self, *args: object) -> None:
            self.closed = True

    span = FakeSpan()
    context = FakeContext(span)
    fake_langfuse = ModuleType("langfuse")
    fake_langfuse.get_client = lambda: type(
        "FakeClient", (), {"start_as_current_observation": lambda *_args, **_kwargs: context}
    )()
    monkeypatch.setitem(sys.modules, "langfuse", fake_langfuse)
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    question = "我叫张三，邮箱是 zhangsan@example.com，换季干燥怎么办？"
    answer = "张三可以尝试屏障修护精华。"
    observer = AgentObservability().start_run("request-1", question)
    observer.record_result(
        "run-1",
        AgentResult(
            answer=answer,
            query_plan=QueryPlan(intent="clarify", sub_questions=[], sources=[], next_action="clarify"),
            model_usage=ModelUsage(skipped_or_fallback_reason="need_clarification"),
            evidence=[],
            bundles=[],
            actions=[],
            requires_confirmation=False,
        ),
        "completed",
    )
    observer.record_failure("run-2", "agent_run_failed")
    observer.close()

    serialized = json.dumps(span.updates, ensure_ascii=False)
    assert question not in serialized
    assert answer not in serialized
    assert "question_sha256" in serialized
    assert any(
        update.get("level") == "ERROR" and update.get("status_message") == "agent_run_failed"
        for update in span.updates
    )
    assert context.closed is True
