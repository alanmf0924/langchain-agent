"""AutoPE Promptfoo 测例：黄金集加人工审核后的线上回流题。"""

from __future__ import annotations

from typing import Any

from autope import load_approved_cases

from tests import generate_tests as generate_golden_tests


def generate_tests(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """保留黄金题，并为候选评测标记必须实际加载候选回答链。"""
    tests = generate_golden_tests(config)
    for test in tests:
        test["vars"]["evaluation_track"] = "autope_candidate_live"

    for case in load_approved_cases():
        variables = {
            "case_id": case["id"],
            "input": case["prompt"],
            "expected_intent": case["expected_intent"],
            "expected_action": case["expected_action"],
            "must_have_evidence": case["must_have_evidence"],
            "expected_evidence_ids": case["expected_evidence_ids_json"],
            "must_not_retrieve_ids": case["must_not_retrieve_ids_json"],
            "evaluation_mode": case["evaluation_mode"],
            "evaluation_track": "autope_candidate_live",
        }
        for key in (
            "expected_bundle_id",
            "expected_bundle_count",
            "expected_requires_confirmation",
            "expected_action_count",
        ):
            if key in case:
                variables[key] = case[key]
        tests.append({"description": f"{case['id']} · {case['dimension']}", "vars": variables})
    return tests
