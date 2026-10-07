"""前台咨询会话的持久化与服务端上下文装配。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.customer.models import CustomerConversation, CustomerConversationMessage
from app.models import AgentResult, QueryPlan
from app.query_planner import plan_question

# 会话轮数上限属于服务端资源与安全策略，前台不展示具体轮数。
MAX_CONVERSATION_CONTEXT_TURNS = 8
MAX_SUMMARY_ITEMS = 6
MAX_SUMMARY_ITEM_CHARS = 24

_NEED_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("干燥紧绷", ("干", "紧", "紧绷", "保湿", "换季")),
    ("泛红敏感", ("泛红", "敏感", "刺痛")),
    ("暗沉焕亮", ("美白", "焕亮", "暗沉")),
    ("油光毛孔", ("油", "油光", "出油", "毛孔", "控油", "闷")),
)
_PREFERENCE_KEYWORDS = ("温和", "清爽", "滋润", "修护", "控油")
_AVOIDANCE_KEYWORDS = ("香精", "酒精", "精油", "a 酸", "刷酸")
_AVOIDANCE_CUES = ("避开", "不含", "不要", "不想", "过敏", "敏感")
_BUDGET_PATTERN = re.compile(r"(?:预算|不超过|以内|控制在)\s*(\d{2,5})\s*(?:元|块)?")


class ConversationMessageResponse(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    phase: Literal["pending", "complete", "failed"]
    result: AgentResult | None = None
    created_at: datetime


class ConversationResponse(BaseModel):
    id: str
    status: Literal["active", "handoff_pending", "archived"]
    handoff_reason: str
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessageResponse] = Field(default_factory=list)


class ConversationHistoryResponse(BaseModel):
    conversation: ConversationResponse | None = None


class ConsultationContextSummary(BaseModel):
    """从用户原话确定性提炼的咨询事实，严格限制容量以控制模型输入。"""

    needs: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    avoidances: list[str] = Field(default_factory=list)
    budget_yuan: int | None = None


@dataclass(frozen=True)
class PreparedConversationRun:
    conversation: CustomerConversation
    agent_question: str
    query_plan: QueryPlan
    should_handoff: bool


def _append_distinct(items: list[str], value: str) -> None:
    if value in items or len(items) >= MAX_SUMMARY_ITEMS:
        return
    clipped = value.strip()[:MAX_SUMMARY_ITEM_CHARS]
    if clipped:
        items.append(clipped)


def _update_context_summary(
    previous: ConsultationContextSummary, question: str
) -> ConsultationContextSummary:
    """只抽取允许进入模型上下文的明确字段，绝不回填历史原文。"""
    normalized = question.lower()
    summary = previous.model_copy(deep=True)
    for label, keywords in _NEED_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            _append_distinct(summary.needs, label)
    for preference in _PREFERENCE_KEYWORDS:
        if preference in normalized:
            _append_distinct(summary.preferences, preference)
    has_avoidance_cue = any(cue in normalized for cue in _AVOIDANCE_CUES)
    for avoidance in _AVOIDANCE_KEYWORDS:
        if avoidance in normalized and has_avoidance_cue:
            _append_distinct(summary.avoidances, avoidance)
    budget_match = _BUDGET_PATTERN.search(normalized)
    if budget_match:
        summary.budget_yuan = int(budget_match.group(1))
    return summary


def _question_with_summary(question: str, summary: ConsultationContextSummary) -> str:
    """组装受限的业务上下文；当前问题保留原文，历史只保留结构化事实。"""
    fields: list[str] = []
    if summary.needs:
        fields.append(f"肤感与诉求：{'、'.join(summary.needs)}")
    if summary.preferences:
        fields.append(f"偏好：{'、'.join(summary.preferences)}")
    if summary.avoidances:
        fields.append(f"避开项：{'、'.join(summary.avoidances)}")
    if summary.budget_yuan is not None:
        fields.append(f"预算：{summary.budget_yuan} 元")
    if not fields:
        return question
    return f"本次咨询已确认的信息：{'；'.join(fields)}\n\n本轮问题：{question}"


def _read_context_summary(value: str) -> ConsultationContextSummary:
    try:
        return ConsultationContextSummary.model_validate_json(value)
    except ValueError:
        # 已上线旧会话或异常历史值不能阻断咨询，下一次写入会自动修复为有效摘要。
        return ConsultationContextSummary()


def _message_response(message: CustomerConversationMessage) -> ConversationMessageResponse:
    result = AgentResult.model_validate_json(message.result_json) if message.result_json else None
    return ConversationMessageResponse(
        id=message.id,
        role=message.role,  # type: ignore[arg-type]  # 数据库约束限定为两个公开角色。
        content=message.content,
        phase=message.phase,  # type: ignore[arg-type]  # 数据库约束限定为三种公开状态。
        result=result,
        created_at=message.created_at,
    )


async def latest_conversation_response(
    session: AsyncSession, customer_id: str
) -> ConversationHistoryResponse:
    conversation = await session.scalar(
        select(CustomerConversation)
        .where(CustomerConversation.customer_id == customer_id)
        .order_by(desc(CustomerConversation.updated_at), desc(CustomerConversation.created_at))
        .limit(1)
    )
    if conversation is None:
        return ConversationHistoryResponse()
    messages = list(
        (
            await session.scalars(
                select(CustomerConversationMessage)
                .where(CustomerConversationMessage.conversation_id == conversation.id)
                .order_by(CustomerConversationMessage.created_at, CustomerConversationMessage.id)
            )
        ).all()
    )
    return ConversationHistoryResponse(
        conversation=ConversationResponse(
            id=conversation.id,
            status=conversation.status,  # type: ignore[arg-type]  # 由迁移 CHECK 约束保护。
            handoff_reason=conversation.handoff_reason,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            messages=[_message_response(item) for item in messages],
        )
    )


async def prepare_conversation_run(
    session: AsyncSession, customer_id: str, conversation_id: str | None, question: str
) -> PreparedConversationRun:
    if conversation_id:
        conversation = await session.scalar(
            select(CustomerConversation).where(
                CustomerConversation.id == conversation_id,
                CustomerConversation.customer_id == customer_id,
            )
        )
        if conversation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="咨询会话不存在")
    else:
        conversation = CustomerConversation(customer_id=customer_id)
        session.add(conversation)
        await session.flush()

    if conversation.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="这段咨询已转入人工接待，请开始新的咨询。",
        )

    previous_count = await session.scalar(
        select(func.count())
        .select_from(CustomerConversationMessage)
        .where(
            CustomerConversationMessage.conversation_id == conversation.id,
            CustomerConversationMessage.role == "user",
        )
    )
    context_summary = _update_context_summary(_read_context_summary(conversation.context_summary_json), question)
    current_question_plan = plan_question(question)
    # 本轮问题先按原文判定范围。否则历史“干燥/预算”等摘要会把“订单到哪了”
    # 错带入推荐链路；只有本轮已确认是护肤咨询时才允许使用历史护理事实。
    if current_question_plan.intent == "out_of_scope":
        agent_question = question
        query_plan = current_question_plan
    else:
        agent_question = _question_with_summary(question, context_summary)
        query_plan = plan_question(agent_question)
    unresolved_turn_count = int(previous_count or 0) + 1
    should_handoff = (
        unresolved_turn_count >= MAX_CONVERSATION_CONTEXT_TURNS
        and query_plan.next_action == "clarify"
    )

    session.add(
        CustomerConversationMessage(
            conversation_id=conversation.id,
            role="user",
            content=question,
            phase="complete",
        )
    )
    conversation.context_summary_json = context_summary.model_dump_json()
    conversation.updated_at = datetime.now(UTC)
    await session.commit()
    return PreparedConversationRun(conversation, agent_question, query_plan, should_handoff)


async def save_assistant_message(
    session: AsyncSession,
    conversation: CustomerConversation,
    content: str,
    phase: Literal["complete", "failed"],
    result: AgentResult | None = None,
) -> None:
    if result is not None and result.requires_handoff:
        conversation.status = "handoff_pending"
        conversation.handoff_reason = "多次澄清后仍无法识别选购目的"
        conversation.handoff_requested_at = datetime.now(UTC)
    session.add(
        CustomerConversationMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=content,
            phase=phase,
            result_json=result.model_dump_json() if result is not None else None,
        )
    )
    conversation.updated_at = datetime.now(UTC)
    await session.commit()
