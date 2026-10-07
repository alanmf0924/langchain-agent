from fastapi.testclient import TestClient

from app.main import app
from app.main import service as api_service
from app.service import SkinAssistantService


def test_trace_locates_intent_tools_evidence_and_safe_output_without_user_plaintext() -> None:
    service = SkinAssistantService()
    service.answer_chain._chain = None
    run_id = "run_trace_recommendation"

    result = service.build_result("换季干燥紧绷，预算 600 元，想做基础修护。", run_id)
    trace = service.get_trace(run_id)

    assert trace is not None
    assert trace.finished_at is not None
    assert not hasattr(trace, "question")
    assert [event.stage for event in trace.events] == [
        "intent",
        "tool",
        "tool",
        "tool",
        "generation",
        "evidence",
        "transaction",
        "completed",
    ]
    evidence_tool = next(
        event
        for event in trace.events
        if event.stage == "tool" and event.details["tool"] == "retrieve_evidence"
    )
    assert evidence_tool.details["result_count"] == len(result.evidence)
    assert evidence_tool.details["sources"][0]["version"]
    assert evidence_tool.details["sources"][0]["source_chunk_id"].endswith("#chunk-1")
    assert trace.events[-1].status == "completed"


def test_safety_trace_stops_before_tools_and_trace_endpoint_can_be_replayed(
    oa_operations_access,
) -> None:
    api_service.answer_chain._chain = None
    run_id = "run_trace_safety"
    api_service.build_result("脸上破溃且持续不适，用什么能治好？", run_id)

    client = TestClient(app)
    response = client.get(f"/api/traces/{run_id}")

    assert response.status_code == 200
    events = response.json()["events"]
    assert [event["stage"] for event in events] == [
        "intent",
        "generation",
        "safety",
        "evidence",
        "transaction",
        "completed",
    ]
    assert events[2]["status"] == "blocked"
    assert client.get("/api/traces/missing_run").status_code == 404
