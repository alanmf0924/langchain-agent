"""可选的 Langfuse 观测适配层；观测故障不能中断业务问答。"""

from __future__ import annotations

import hashlib
import logging
import os
from contextlib import AbstractContextManager
from typing import Any

from app.models import AgentResult

logger = logging.getLogger(__name__)


class AgentRunObserver:
    """仅记录可复核的业务摘要，绝不发送用户原文或模型思维链。"""

    def record_result(self, run_id: str, result: AgentResult, status: str) -> None:
        raise NotImplementedError

    def record_failure(self, run_id: str, reason: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class _NoopAgentRunObserver(AgentRunObserver):
    """默认实现，确保未配置 Langfuse 时业务行为不变。"""

    def record_result(self, run_id: str, result: AgentResult, status: str) -> None:
        del run_id, result, status

    def record_failure(self, run_id: str, reason: str) -> None:
        del run_id, reason

    def close(self) -> None:
        return None


class _LangfuseAgentRunObserver(AgentRunObserver):
    def __init__(self, context: AbstractContextManager[Any], span: Any) -> None:
        self._context = context
        self._span = span
        self._closed = False

    def record_result(self, run_id: str, result: AgentResult, status: str) -> None:
        self._span.update(
            output={
                "run_id": run_id,
                "status": status,
                "intent": result.query_plan.intent,
                "next_action": result.query_plan.next_action,
                "evidence_ids": [item.id for item in result.evidence],
                "bundle_ids": [item.bundle_id for item in result.bundles],
                "action_count": len(result.actions),
                "requires_confirmation": result.requires_confirmation,
                "model_attempted": result.model_usage.provider_attempted,
                "model_call_count": result.model_usage.model_call_count,
                "model": result.model_usage.model,
                "fallback_reason": result.model_usage.skipped_or_fallback_reason,
            }
        )

    def record_failure(self, run_id: str, reason: str) -> None:
        self._span.update(output={"run_id": run_id, "status": "failed", "reason": reason})

    def close(self) -> None:
        if not self._closed:
            self._context.__exit__(None, None, None)
            self._closed = True


class AgentObservability:
    """延迟加载 Langfuse SDK，避免可选观测依赖改变线上核心可用性。"""

    def __init__(self) -> None:
        self.enabled = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"
        self.environment = os.getenv("LANGFUSE_ENVIRONMENT", os.getenv("APP_ENV", "development"))

    @staticmethod
    def _question_summary(question: str) -> dict[str, Any]:
        return {
            "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
            "question_length": len(question),
        }

    def start_run(self, request_id: str, question: str) -> AgentRunObserver:
        if not self.enabled:
            return _NoopAgentRunObserver()
        if not os.getenv("LANGFUSE_PUBLIC_KEY") or not os.getenv("LANGFUSE_SECRET_KEY"):
            logger.warning("Langfuse 已启用但缺少凭据；本次问答不写观测")
            return _NoopAgentRunObserver()
        try:
            from langfuse import get_client

            client = get_client()
            context = client.start_as_current_observation(as_type="span", name="skin-assistant.agent-run")
            span = context.__enter__()
            span.update(
                input=self._question_summary(question),
                metadata={
                    "request_id": request_id,
                    "environment": self.environment,
                    "io_policy": "hash-and-structured-summary-only",
                },
            )
            return _LangfuseAgentRunObserver(context, span)
        except Exception:  # 第三方观测绝不能影响用户请求。
            logger.exception("Langfuse 观测初始化失败；业务问答继续执行")
            return _NoopAgentRunObserver()
