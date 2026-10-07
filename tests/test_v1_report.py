import json
from pathlib import Path

from eval.v1_report import build_v1_golden_report, export_report, render_markdown


def test_v1_golden_report_executes_all_cases_without_model_or_trade_writes(tmp_path: Path) -> None:
    report = build_v1_golden_report()
    cases = json.loads((Path(__file__).parents[1] / "eval" / "golden_questions.json").read_text())

    assert report["summary"]["total_cases"] == len(cases)
    assert report["summary"]["failed_cases"] == 0
    assert report["execution_mode"]["external_model_calls"] == 0
    assert report["execution_mode"]["transaction_writes_executed"] == 0
    assert report["execution_mode"]["name"] == "deterministic_local_no_external_model"
    assert all(not case["model_usage"][0]["provider_attempted"] for case in report["cases"])
    assert all(not case["transaction_action"]["executed"] for case in report["cases"])

    safety_case = next(case for case in report["cases"] if case["case_id"] == "safety-01")
    assert safety_case["security_action"]["action"] == "stop_recommendation"
    assert safety_case["tool_audit"] == []

    recommendation = next(case for case in report["cases"] if case["case_id"] == "simple-01")
    assert recommendation["evidence"]
    assert {entry["action"] for entry in recommendation["tool_audit"]} == {
        "search_catalog",
        "get_realtime_price_stock",
        "retrieve_evidence",
    }

    json_path = export_report(report, tmp_path / "report.json", "json")
    assert json.loads(json_path.read_text(encoding="utf-8"))["summary"]["total_cases"] == len(cases)

    markdown = render_markdown(report)
    assert "# 澄肌护肤选购助手 V1 黄金问题评估报告" in markdown
    assert "### simple-01" in markdown
    assert "证据 ID / 版本" in markdown
