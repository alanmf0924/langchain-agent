from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from app.customer.conversations import (
    ConsultationContextSummary,
    _question_with_summary,
    _update_context_summary,
    prepare_conversation_run,
)
from app.customer.database import SessionLocal
from app.customer.database import engine as customer_engine
from app.customer.models import CustomerBase, CustomerConversation
from app.main import app


async def reset_customer_conversations() -> None:
    async with customer_engine.begin() as connection:
        await connection.run_sync(CustomerBase.metadata.drop_all)
        await connection.run_sync(CustomerBase.metadata.create_all)


def sse_events(body: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for frame in body.split("\n\n"):
        data = next((line[5:].strip() for line in frame.splitlines() if line.startswith("data:")), "")
        if data:
            events.append(json.loads(data))
    return events


def register_and_login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/customer/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.test",
            "display_name": username,
            "password": "CorrectHorseBattery1",
        },
    )
    assert response.status_code == 202
    login = client.post(
        "/api/customer/auth/login",
        json={"username": username, "password": "CorrectHorseBattery1"},
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_structured_context_summary_keeps_facts_without_replaying_history() -> None:
    """模型输入只携带短事实摘要，上一轮的原文不会被重新拼接。"""
    old_question = "换季时脸颊干燥紧绷，预算 600 元以内，请避开香精。" + "不应重复" * 80
    summary = _update_context_summary(ConsultationContextSummary(), old_question)
    agent_question = _question_with_summary("请给我温和一些的建议", summary)

    assert summary.needs == ["干燥紧绷"]
    assert summary.avoidances == ["香精"]
    assert summary.budget_yuan == 600
    assert old_question not in agent_question
    assert "不应重复" not in agent_question
    assert "预算：600 元" in agent_question
    assert "避开项：香精" in agent_question
    assert agent_question.endswith("本轮问题：请给我温和一些的建议")


async def prepare_and_read_persisted_summary() -> tuple[str, str]:
    async with SessionLocal() as session:
        conversation = CustomerConversation(customer_id="summary-test-customer")
        session.add(conversation)
        await session.flush()
        prepared = await prepare_conversation_run(
            session,
            customer_id=conversation.customer_id,
            conversation_id=conversation.id,
            question="换季干燥紧绷，预算 600 元以内，避开香精。",
        )
        persisted = await session.get(CustomerConversation, conversation.id)
        assert persisted is not None
        return prepared.agent_question, persisted.context_summary_json


def test_structured_context_summary_is_persisted_with_the_conversation() -> None:
    asyncio.run(reset_customer_conversations())
    agent_question, persisted_summary = asyncio.run(prepare_and_read_persisted_summary())
    summary = ConsultationContextSummary.model_validate_json(persisted_summary)

    assert summary.needs == ["干燥紧绷"]
    assert summary.avoidances == ["香精"]
    assert summary.budget_yuan == 600
    assert "本次咨询已确认的信息" in agent_question


async def prepare_order_question_with_existing_skincare_summary() -> tuple[str, str, str]:
    async with SessionLocal() as session:
        conversation = CustomerConversation(customer_id="order-scope-test-customer")
        conversation.context_summary_json = ConsultationContextSummary(
            needs=["干燥紧绷"], budget_yuan=600
        ).model_dump_json()
        session.add(conversation)
        await session.flush()
        prepared = await prepare_conversation_run(
            session,
            customer_id=conversation.customer_id,
            conversation_id=conversation.id,
            question="我的订单怎么查询？",
        )
        return prepared.agent_question, prepared.query_plan.intent, prepared.query_plan.next_action


def test_order_question_does_not_inherit_skincare_summary_into_recommendation() -> None:
    """历史摘要只能补充本轮护肤咨询，不能改变订单问题的业务边界。"""
    asyncio.run(reset_customer_conversations())
    agent_question, intent, next_action = asyncio.run(
        prepare_order_question_with_existing_skincare_summary()
    )

    assert agent_question == "我的订单怎么查询？"
    assert intent == "out_of_scope"
    assert next_action == "stop"


def test_order_agent_endpoint_returns_direct_scope_refusal_without_recommendation() -> None:
    """SSE 入口也必须维持边界，不能仅在内部 Service 层表现正确。"""
    asyncio.run(reset_customer_conversations())
    with TestClient(app) as client:
        headers = register_and_login(client, "order-scope-owner")
        response = client.post(
            "/api/agent/runs",
            json={"question": "我的订单怎么查询？"},
            headers=headers,
        )

    assert response.status_code == 200
    events = sse_events(response.text)
    event_types = [event["type"] for event in events]
    plan = next(event["payload"] for event in events if event["type"] == "retrieval_plan")
    completed = next(event["payload"] for event in events if event["type"] == "run_completed")

    assert "tool_started" not in event_types
    assert "approval_required" not in event_types
    assert plan["intent"] == "out_of_scope"
    assert plan["sources"] == []
    assert completed["result"]["bundles"] == []
    assert completed["result"]["actions"] == []
    assert "订单中心" in completed["result"]["answer"]


def test_customer_conversation_survives_refresh_and_unresolved_context_requests_handoff() -> None:
    """会话只归属当前客户；第八次仍无法识别目的时不再调用自动推荐。"""
    asyncio.run(reset_customer_conversations())
    with TestClient(app) as client:
        headers = register_and_login(client, "conversation-owner")
        conversation_id = ""
        for _ in range(7):
            response = client.post(
                "/api/agent/runs",
                json={"question": "买什么", **({"conversation_id": conversation_id} if conversation_id else {})},
                headers=headers,
            )
            assert response.status_code == 200
            events = sse_events(response.text)
            started = next(event for event in events if event["type"] == "run_started")
            conversation_id = str(started["payload"]["conversation_id"])
            assert "handoff_required" not in [event["type"] for event in events]

        handoff = client.post(
            "/api/agent/runs",
            json={"question": "买什么", "conversation_id": conversation_id},
            headers=headers,
        )
        assert handoff.status_code == 200
        handoff_events = sse_events(handoff.text)
        assert "handoff_required" in [event["type"] for event in handoff_events]
        completed = next(event for event in handoff_events if event["type"] == "run_completed")
        assert completed["payload"]["result"]["requires_handoff"] is True

        restored = client.get("/api/customer/conversations/latest", headers=headers)
        assert restored.status_code == 200
        conversation = restored.json()["conversation"]
        assert conversation["id"] == conversation_id
        assert conversation["status"] == "handoff_pending"
        assert len(conversation["messages"]) == 16
        assert conversation["messages"][-1]["result"]["requires_handoff"] is True

        second_headers = register_and_login(client, "conversation-other")
        forbidden = client.post(
            "/api/agent/runs",
            json={"question": "换季干燥紧绷", "conversation_id": conversation_id},
            headers=second_headers,
        )
        assert forbidden.status_code == 404
