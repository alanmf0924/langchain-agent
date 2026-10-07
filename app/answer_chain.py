from __future__ import annotations

import os
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from app.models import ModelUsage

SYSTEM_PROMPT = """你是澄肌护肤选购助手。只能依据提供的商品资料回答，不能编造成分、功效、价格或库存。
不要执行交易、改价、改库存、发券或积分操作；这些操作由服务端的权限层处理。
用简洁中文分段说明使用顺序、推荐依据与注意事项。输出面向消费者的纯文本，禁止使用 Markdown 标记、标题、列表符号或代码块。"""
MAX_EVIDENCE_ITEMS = 3
MAX_EVIDENCE_CHARS_PER_ITEM = 240
DEFAULT_MAX_OUTPUT_TOKENS = 300
DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS = 8


@dataclass(frozen=True)
class GeneratedAnswer:
    """模型或确定性降级后的回答，以及可公开的成本元数据。"""

    text: str
    usage: ModelUsage


class GroundedAnswerChain:
    """LangChain LCEL 回答链；LLM 不参与工具授权或交易决策。"""

    def __init__(self) -> None:
        # 本地运行时以项目 .env 为准；测试显式注入隔离数据库，不能被本机密钥配置覆盖。
        load_dotenv(override=os.getenv("APP_ENV") != "test")
        self.model_name = os.getenv("OPENAI_MODEL", "deepseek-v4-pro")
        self.max_output_tokens = int(os.getenv("MODEL_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS))
        self.request_timeout_seconds = self._bounded_env_int(
            "MODEL_REQUEST_TIMEOUT_SECONDS", DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS, 1, 30
        )
        self._chain = self._build_if_configured()

    @staticmethod
    def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
        """拒绝异常配置，避免无限等待或意外放大外部模型资源消耗。"""
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError as error:
            raise ValueError(f"{name} 必须是整数") from error
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
        return value

    def _build_if_configured(self):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            # 没有模型配置也保留业务流程，以确定性回答完成本地联调和策略测试。
            return None
        # DeepSeek 使用 OpenAI Chat Completions 兼容协议，因此复用 ChatOpenAI 客户端。
        model = ChatOpenAI(
            model=os.getenv("OPENAI_MODEL", "deepseek-v4-pro"),
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL"),
            temperature=0,
            max_tokens=self.max_output_tokens,
            timeout=self.request_timeout_seconds,
            # 不让 SDK 隐式重试；一次运行的调用预算由 LangGraph/Service 显式控制。
            max_retries=0,
        )
        prompt = ChatPromptTemplate.from_messages(
            [
                # 系统提示只限制回答边界；交易权限始终由 policy.py 的确定性规则控制。
                ("system", SYSTEM_PROMPT),
                ("human", "用户问题：{question}\n\n已审核资料：\n{evidence}"),
            ]
        )
        # 保留 AIMessage，才能读取服务商实际返回的 token usage；StrOutputParser 会丢失该信息。
        return prompt | model

    @staticmethod
    def _fallback_answer(evidence: list[dict[str, Any]]) -> str:
        if len(evidence) == 1:
            return (
                "根据已审核的在售资料，这件单品可作为本次替代建议。"
                "请按商品资料中的用法使用，并留意注意事项；出现不适请停止使用并线下咨询专业人士。"
            )
        return "根据已审核的在售资料，建议按温和洁面、修护精华、保湿面霜的顺序护理。首次使用先做局部测试；出现破溃、持续不适或严重红肿时请停止使用并线下咨询专业人士。"

    @staticmethod
    def plain_text(text: str) -> str:
        """清除兼容模型偶尔返回的 Markdown，保证 API 对消费者始终返回可直接展示的文本。"""
        cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
        cleaned = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cleaned)
        cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
        cleaned = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", cleaned)
        cleaned = re.sub(r"(?m)^\s*[-*+]\s+", "", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    @staticmethod
    def _usage_from_message(message: Any) -> tuple[int | None, int | None, int | None, bool]:
        """兼容 OpenAI 与常见兼容服务的两种 usage 字段命名。"""
        raw = getattr(message, "usage_metadata", None) or getattr(message, "response_metadata", {}).get(
            "token_usage", {}
        )
        if not raw:
            return None, None, None, False
        input_tokens = raw.get("input_tokens", raw.get("prompt_tokens"))
        output_tokens = raw.get("output_tokens", raw.get("completion_tokens"))
        total_tokens = raw.get("total_tokens")
        return input_tokens, output_tokens, total_tokens, True

    @staticmethod
    def _evidence_text(evidence: list[dict[str, Any]]) -> tuple[str, int, int]:
        """在模型调用前硬性裁剪证据，控制上下文 token，而不影响原始证据回传。"""
        selected = evidence[:MAX_EVIDENCE_ITEMS]
        lines = [
            f"- {item['title']}（{item['version']}）：{item['quote'][:MAX_EVIDENCE_CHARS_PER_ITEM]}"
            for item in selected
        ]
        text = "\n".join(lines)
        return text, len(selected), len(text)

    def answer(
        self, question: str, evidence: list[dict[str, Any]], allow_model_call: bool = True
    ) -> GeneratedAnswer:
        evidence_text, evidence_count, evidence_chars = self._evidence_text(evidence)
        if not allow_model_call:
            return GeneratedAnswer(
                self._fallback_answer(evidence),
                ModelUsage(
                    evidence_count=evidence_count,
                    evidence_chars=evidence_chars,
                    skipped_or_fallback_reason="model_call_budget_exhausted",
                ),
            )
        if self._chain is None:
            return GeneratedAnswer(
                self._fallback_answer(evidence),
                ModelUsage(
                    evidence_count=evidence_count,
                    evidence_chars=evidence_chars,
                    skipped_or_fallback_reason="model_not_configured",
                ),
            )
        # 仅拼接已经过 Catalog 审核的证据，避免模型看到未上架或未审核资料。
        started = perf_counter()
        try:
            message = self._chain.invoke({"question": question, "evidence": evidence_text})
            input_tokens, output_tokens, total_tokens, usage_reported = self._usage_from_message(message)
            return GeneratedAnswer(
                self.plain_text(str(message.content)),
                ModelUsage(
                    model=getattr(message, "response_metadata", {}).get("model_name", self.model_name),
                    provider_attempted=True,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    usage_reported=usage_reported,
                    elapsed_ms=round((perf_counter() - started) * 1000),
                    output_token_limit=self.max_output_tokens,
                    model_call_count=1,
                    evidence_count=evidence_count,
                    evidence_chars=evidence_chars,
                ),
            )
        except Exception as error:  # noqa: BLE001 - 第三方模型故障必须降级为安全的确定性回答。
            # 模型故障不能中断可验证的业务推荐和确认流程，也不能伪造模型输出。
            is_timeout = isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower()
            return GeneratedAnswer(
                "已根据已审核的在售资料完成推荐。请查看资料版本和使用顺序；首次使用建议局部测试。",
                ModelUsage(
                    model=self.model_name,
                    provider_attempted=True,
                    elapsed_ms=round((perf_counter() - started) * 1000),
                    output_token_limit=self.max_output_tokens,
                    model_call_count=1,
                    evidence_count=evidence_count,
                    evidence_chars=evidence_chars,
                    skipped_or_fallback_reason="model_timeout" if is_timeout else "model_provider_error",
                ),
            )
