"""AutoPE 控制面回归测试；不调用 DSPy、Langfuse 或模型。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from assertions import get_assert
from autope import (
    AutoPEError,
    build_candidate_manifest,
    build_review_queue,
    fetch_langfuse_observations,
    load_approved_cases,
    load_dataset_splits,
    register_release_candidate,
    select_candidate,
    validate_reviewed_candidate,
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

    def test_langfuse_export_reads_summary_spans(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"data": [], "meta": {"cursor": null}}'

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "export.json"
            with (
                patch.dict(
                    os.environ,
                    {
                        "LANGFUSE_BASE_URL": "https://cloud.langfuse.com",
                        "LANGFUSE_PUBLIC_KEY": "pk-test",
                        "LANGFUSE_SECRET_KEY": "sk-test",
                    },
                    clear=False,
                ),
                patch("autope.urllib.request.urlopen", return_value=FakeResponse()) as urlopen,
            ):
                fetch_langfuse_observations(
                    "2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z", output
                )

            request = urlopen.call_args.args[0]
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            self.assertEqual(query["type"], ["SPAN"])

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

    def test_release_registration_requires_approved_disjoint_splits_and_reviewed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_dir = root / "review"
            review_dir.mkdir()
            brief_path = root / "brief.json"
            split_path = root / "splits.json"
            policy_path = root / "release-policy.json"
            manifest_path = review_dir / "candidate.json"
            promotion_path = root / "promotion.json"
            output = root / "release.json"
            approved_brief = {"schema_version": "autope-brief-v1", **self.brief}
            brief_path.write_text(json.dumps(approved_brief), encoding="utf-8")
            split_path.write_text(
                json.dumps(
                    {
                        "schema_version": "autope-dataset-splits-v1",
                        "approval": {
                            "status": "approved",
                            "approved_by": "qa-owner",
                            "approved_at": "2026-10-07T00:00:00Z",
                            "review_ticket": "AUTOPE-101",
                        },
                        "splits": {
                            "training": ["simple-01"],
                            "validation": ["simple-02"],
                            "challenge": ["safety-01"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            policy_path.write_text(
                json.dumps(
                    {
                        "schema_version": "autope-release-policy-v1",
                        "approval": {
                            "status": "approved",
                            "approved_by": "release-owner",
                            "approved_at": "2026-10-07T00:00:00Z",
                            "review_ticket": "AUTOPE-103",
                        },
                        "canary": {"traffic_percent": 5, "observation_minutes": 60},
                        "rollback": {
                            "owner": "on-call-owner",
                            "runbook_url": "https://example.test/runbooks/autope-rollback",
                            "strategy": "immediate_revert_to_last_approved_prompt_version",
                            "target": "last_approved_prompt_version",
                            "execution": "manual_confirmed",
                            "steps": [
                                "stop canary",
                                "restore approved prompt",
                                "verify active prompt hash",
                                "run deterministic gates",
                            ],
                        },
                        "rollback_thresholds": {"error_rate": 0.01, "p95_latency_ms": 2500},
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("autope.DEFAULT_CANDIDATE_REVIEW_DIR", review_dir),
                patch("autope.DEFAULT_BRIEF", brief_path),
                patch("autope.DEFAULT_DATASET_SPLITS", split_path),
                patch("autope._baseline_system_prompt", return_value=self.baseline),
            ):
                manifest = build_candidate_manifest(
                    approved_brief,
                    [{"system_prompt": "evidence only\nno transaction\nplain text\nbe concise", "hypothesis": "shorter"}],
                    self.baseline,
                )
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                candidate_id = manifest["candidates"][0]["candidate_id"]
                validated = validate_reviewed_candidate(manifest_path, candidate_id)
                self.assertEqual(validated["candidate_id"], candidate_id)
                self.assertEqual(load_dataset_splits()["splits"]["challenge"], ["safety-01"])
                promotion_path.write_text(
                    json.dumps(
                        select_candidate(
                            _write_promptfoo_fixture(root / "baseline.json", None, False),
                            _write_promptfoo_fixture(root / "candidate-results.json", candidate_id, True),
                            root / "promotion-output.json",
                            max_cost=2.0,
                        )
                    ),
                    encoding="utf-8",
                )
                record = register_release_candidate(
                    promotion_path, manifest_path, candidate_id, split_path, policy_path, output
                )
            self.assertEqual(record["status"], "pending_human_release_approval")

    def test_dataset_splits_reject_cross_split_case_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            brief_path = root / "brief.json"
            split_path = root / "splits.json"
            brief_path.write_text(
                json.dumps({"schema_version": "autope-brief-v1", **self.brief}), encoding="utf-8"
            )
            split_path.write_text(
                json.dumps(
                    {
                        "schema_version": "autope-dataset-splits-v1",
                        "approval": {
                            "status": "approved",
                            "approved_by": "qa-owner",
                            "approved_at": "2026-10-07T00:00:00Z",
                            "review_ticket": "AUTOPE-102",
                        },
                        "splits": {
                            "training": ["simple-01"],
                            "validation": ["simple-01"],
                            "challenge": ["safety-01"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AutoPEError, "不得跨集合复用"):
                load_dataset_splits(split_path, brief_path)

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


def _write_promptfoo_fixture(path: Path, candidate_id: str | None, applied: bool) -> Path:
    path.write_text(json.dumps(_promptfoo_fixture(candidate_id, applied)), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
