"""把业务仓库的黄金集转换成 Promptfoo 测例，不在此复制题目。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_FILE = PROJECT_ROOT / "eval" / "golden_questions.json"


def generate_tests(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """输出一个 Promptfoo 测例一条黄金题，保留每条题目的可审计期望。"""
    del config  # Promptfoo 兼容签名；黄金集的完整覆盖不允许调用方静默截断。
    cases = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise TypeError("eval/golden_questions.json 必须是 JSON 数组")

    tests: list[dict[str, Any]] = []
    optional_fields = (
        "evaluation_mode",
        "expected_bundle_id",
        "expected_bundle_count",
        "expected_fallback_reason",
        "expected_requires_confirmation",
        "expected_action_count",
    )
    for case in cases:
        variables: dict[str, Any] = {
            "case_id": case["id"],
            "input": case["prompt"],
            "expected_intent": case["expected_intent"],
            "expected_action": case["expected_action"],
            "must_have_evidence": case["must_have_evidence"],
            # Promptfoo 会将 list 类型变量当作测试矩阵展开；序列化后每条黄金题只执行一次。
            "expected_evidence_ids": json.dumps(case.get("expected_evidence_ids", [])),
            "must_not_retrieve_ids": json.dumps(case.get("must_not_retrieve_ids", [])),
            "evaluation_mode": case.get("evaluation_mode", "read_only"),
        }
        # Promptfoo vars 不接受 null；缺省字段表示该条 V1 老题仍只校验原有契约。
        variables.update({key: case[key] for key in optional_fields if key in case})
        tests.append({"description": f"{case['id']} · {case['dimension']}", "vars": variables})
    return tests
