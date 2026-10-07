import json
from pathlib import Path

from eval.retrieval_report import (
    _reranker_decision,
    build_retrieval_report,
    export_report,
    render_markdown,
)


def test_retrieval_report_measures_golden_evidence_and_defers_reranker_for_small_corpus(
    tmp_path: Path,
) -> None:
    report = build_retrieval_report()
    cases = json.loads((Path(__file__).parents[1] / "eval" / "golden_questions.json").read_text())

    assert report["scope"]["golden_cases"] == len(cases)
    assert report["scope"]["evaluated_evidence_cases"] == sum(
        bool(case["expected_evidence_ids"]) for case in cases
    )
    assert report["scope"]["searchable_document_count"] == 8
    assert report["scope"]["globally_blocked_document_ids"] == ["chunk_offline_v10"]
    assert report["metrics"]["violation_retrieval_rate"] == 0.0
    # 题集只认可主证据；报告应暴露额外引用，而不是把它们当作自动正确。
    assert 0 < report["metrics"]["citation_correctness"] < 1
    assert report["reranker_gate"]["should_add_reranker"] is False
    assert report["reranker_gate"]["decision"] == "not_now"

    first = report["cases"][0]
    assert first["expected_evidence_ids"]
    assert "chunk_offline_v10" not in first["ranked_evidence_ids_top5"]
    assert "chunk_offline_v10" not in first["agent_citation_ids_top3"]

    json_path = export_report(report, tmp_path / "retrieval.json", "json")
    assert json.loads(json_path.read_text(encoding="utf-8"))["metrics"]["mrr"] >= 0
    markdown = render_markdown(report)
    assert "# 检索基线与 Reranker 准入报告" in markdown
    assert "暂不接入" in markdown


def test_reranker_gate_only_recommends_when_recall_is_sufficient_but_top3_is_weak() -> None:
    metrics = {
        "recall_at_5": 1.0,
        "mrr": 0.7,
        "recall_at_3": 0.8,
        "first_hit_at_3": 0.85,
        "rank_4_5_rescue_rate": 0.2,
    }

    recommendation = _reranker_decision(20, metrics, violation_retrieval_rate=0.0)
    assert recommendation["should_add_reranker"] is True
    assert recommendation["decision"] == "recommend"

    blocked = _reranker_decision(20, metrics, violation_retrieval_rate=0.01)
    assert blocked["should_add_reranker"] is False
    assert blocked["decision"] == "block"
