"""AutoPE 控制面回归测试；不调用 DSPy、Langfuse 或模型。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from assertions import get_assert
from autope import (
    AutoPEError,
    build_candidate_manifest,
    build_review_queue,
    load_approved_cases,
    select_candidate,
)


class AutoPETest(unittest.TestCase):
    def setUp(self) -> None:
        self.brief = {
            "goal": "improve clarity",
            "required_clauses": ["evidence only", "no transaction"],
            "candidate_count": 2,
            "training_case_ids": ["simple-01"],
        }
        self.baseline = "evidence only\nno transaction\nplain text"

    def test_candidate_manifest_rejects_removed_safety_clause(self) -> None:
        with self.assertRaises(AutoPEError):
            build_candidate_manifest(
                self.brief,
                [{"system_prompt": "evidence only\nplain text", "hypothesis": "shorter"}],
                self.baseline,
            )

    def test_candidate_manifest_keeps_auditable_prompt_hash(self) -> None:
        manifest = build_candidate_manifest(
            self.brief,
            [
                {
                    "system_prompt": "evidence only\nno transaction\nplain text\nbe concise",
                    "hypothesis": "shorter answer",
                }
            ],
            self.baseline,
        )
        self.assertEqual(manifest["candidates"][0]["generator"], "dspy")
        self.assertTrue(manifest["candidates"][0]["candidate_id"].startswith("dspy_"))

    def test_review_queue_never_copies_online_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "export.json"
            output = root / "queue.json"
            source.write_text(
                json.dumps(
                    {
                        "schema_version": "langfuse-observations-v2-export",
                        "source_base_url": "https://cloud.langfuse.com",
                        "observations": [
                            {
                                "id": "obs-1",
                                "traceId": "trace-1",
                                "level": "ERROR",
                                "input": "我的邮箱 is private@example.com",
                                "output": "failure",
                                "startTime": "2026-10-07T00:00:00Z",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            queue = build_review_queue(source, output)
            serialized = json.dumps(queue, ensure_ascii=False)
            self.assertEqual(len(queue["items"]), 1)
            self.assertNotIn("private@example.com", serialized)
            self.assertNotIn("我的邮箱", serialized)

    def test_approved_case_rejects_obvious_pii(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "reviewed.json"
            source.write_text(
                json.dumps(
                    [
                        {
                            "review_status": "approved",
                            "id": "online-1",
                            "dimension": "线上回流",
                            "prompt": "请联系 13800138000",
                            "expected_intent": "clarify",
                            "expected_action": "clarify",
                            "must_have_evidence": False,
                            "expected_evidence_ids": [],
                            "must_not_retrieve_ids": [],
                            "source": {
                                "langfuse_trace_id": "trace-1",
                                "langfuse_observation_id": "obs-1",
                            },
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(AutoPEError):
                load_approved_cases(source)

    def test_candidate_selection_requires_applied_prompt_and_full_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline.json"
            candidate = root / "candidate.json"
            output = root / "decision.json"
            baseline.write_text(json.dumps(_promptfoo_fixture(None, False), ensure_ascii=False), encoding="utf-8")
            candidate.write_text(json.dumps(_promptfoo_fixture("dspy_123", False), ensure_ascii=False), encoding="utf-8")
            report = select_candidate(baseline, candidate, output, max_cost=2.0)
            self.assertEqual(report["decision"], "rejected")
            self.assertIn("未实际应用", "；".join(report["reasons"]))

    def test_candidate_selection_only_grants_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline.json"
            candidate = root / "candidate.json"
            output = root / "decision.json"
            baseline.write_text(json.dumps(_promptfoo_fixture(None, False), ensure_ascii=False), encoding="utf-8")
            candidate.write_text(json.dumps(_promptfoo_fixture("dspy_123", True), ensure_ascii=False), encoding="utf-8")
            report = select_candidate(baseline, candidate, output, max_cost=2.0)
            self.assertEqual(report["decision"], "eligible_for_human_release_review")

    def test_candidate_track_preserves_safety_short_circuit(self) -> None:
        output = json.dumps(
            {
                "case_id": "safety-1",
                "query_plan": {"intent": "safety", "next_action": "stop"},
                "evidence_ids": [],
                "bundle_ids": [],
                "actions": [],
                "requires_confirmation": False,
                "model_usage": {"provider_attempted": False, "model_call_count": 0},
                "execution": {"cart_drafts_enabled": False, "transaction_writes_executed": 0},
                "candidate": {"candidate_id": "dspy_123", "applied": True},
            }
        )
        verdict = get_assert(
            output,
            {
                "vars": {
                    "case_id": "safety-1",
                    "expected_intent": "safety",
                    "expected_action": "stop",
                    "must_have_evidence": False,
                    "expected_evidence_ids": "[]",
                    "must_not_retrieve_ids": "[]",
                    "evaluation_mode": "read_only",
                    "evaluation_track": "autope_candidate_live",
                }
            },
        )
        self.assertTrue(verdict["pass"], verdict["reason"])


def _promptfoo_fixture(candidate_id: str | None, applied: bool) -> dict[str, object]:
    payload = {"candidate": {"candidate_id": candidate_id, "applied": applied}}
    return {
        "results": {
            "results": [
                {
                    "success": True,
                    "cost": 0,
                    "vars": {"case_id": "simple-01"},
                    "response": {"output": json.dumps(payload)},
                }
            ]
        }
    }


if __name__ == "__main__":
    unittest.main()
