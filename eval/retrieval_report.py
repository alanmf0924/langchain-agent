"""可复跑的检索基线评估与 Reranker 准入判定。

评测直接复用 ``KnowledgeBase.search`` 的审核、在售过滤和排序逻辑，并用同一
``SkinAssistantService`` 取得实际上屏的 Top 3 引用。它不会调用外部 Embedding
或聊天模型，也不会创建购物车草稿。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.embeddings import LexicalOnlyEmbedder
from app.service import SkinAssistantService

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDEN_FILE = ROOT / "eval" / "golden_questions.json"
DEFAULT_OUTPUT_FILE = ROOT / "eval" / "reports" / "retrieval_baseline_report.md"
ReportFormat = Literal["markdown", "json"]

# 20–50 份是当前阶段验证排序价值的资料量窗口；低于此值先扩充并标注真实资料。
MIN_RERANKER_CORPUS_SIZE = 20
MAX_RERANKER_CORPUS_SIZE = 50
MIN_RECALL_AT_5_FOR_RERANKING = 0.95
MIN_FIRST_HIT_AT_3 = 0.90
MIN_RECALL_AT_3 = 0.85


def _unique_ids(items: list[Any]) -> list[str]:
    """按文档去重，避免一个长文多切片稀释文档级 Recall 与 MRR。"""
    return list(dict.fromkeys(item.id for item in items))


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _load_cases(golden_file: Path) -> list[dict[str, Any]]:
    raw = json.loads(golden_file.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError("黄金问题文件必须是 JSON 数组")
    case_ids: set[str] = set()
    for case in raw:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in case_ids:
            raise ValueError("黄金问题必须使用唯一的 id")
        case_ids.add(case_id)
        expected = case.get("expected_evidence_ids")
        blocked = case.get("must_not_retrieve_ids")
        if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
            raise ValueError(f"{case_id} 缺少合法 expected_evidence_ids")
        if not isinstance(blocked, list) or not all(isinstance(item, str) for item in blocked):
            raise ValueError(f"{case_id} 缺少合法 must_not_retrieve_ids")
        if case.get("must_have_evidence") != bool(expected):
            raise ValueError(f"{case_id} 的证据开关与 expected_evidence_ids 不一致")
    return raw


def _reranker_decision(
    searchable_document_count: int,
    metrics: dict[str, float],
    violation_retrieval_rate: float,
) -> dict[str, str | bool]:
    """只在过滤正常、召回充足且 Top 3 确有排序缺口时建议加入 Reranker。"""
    if not MIN_RERANKER_CORPUS_SIZE <= searchable_document_count <= MAX_RERANKER_CORPUS_SIZE:
        return {
            "should_add_reranker": False,
            "decision": "not_now",
            "reason": (
                f"可检索语料为 {searchable_document_count} 份，不在 "
                f"{MIN_RERANKER_CORPUS_SIZE}–{MAX_RERANKER_CORPUS_SIZE} 份的验证窗口；"
                "先接入并审核真实资料，再测排序收益。"
            ),
        }
    if violation_retrieval_rate > 0:
        return {
            "should_add_reranker": False,
            "decision": "block",
            "reason": "检测到未审核、下架或题目禁用资料被召回；先修复过滤，不能用重排掩盖违规。",
        }
    if metrics["recall_at_5"] < MIN_RECALL_AT_5_FOR_RERANKING:
        return {
            "should_add_reranker": False,
            "decision": "not_now",
            "reason": "Recall@5 未达到 0.95；先改进召回/切块/标签，Reranker 不能找回未召回资料。",
        }
    top3_weak = (
        metrics["first_hit_at_3"] < MIN_FIRST_HIT_AT_3
        or metrics["recall_at_3"] < MIN_RECALL_AT_3
    )
    if top3_weak and metrics["rank_4_5_rescue_rate"] > 0:
        return {
            "should_add_reranker": True,
            "decision": "recommend",
            "reason": "Top 5 召回已达门槛，但正确资料仍有落在第 4–5 位的可重排空间；建议实验混合召回 Top 20 → Reranker → Top 3。",
        }
    return {
        "should_add_reranker": False,
        "decision": "not_needed",
        "reason": "Top 5 召回和 Top 3 排序均达到门槛，当前不应增加 Reranker 的成本与复杂度。",
    }


def build_retrieval_report(golden_file: Path = DEFAULT_GOLDEN_FILE) -> dict[str, Any]:
    """在当前黄金集上计算文档级 Recall@5、MRR、引用正确率和违规召回率。"""
    cases = _load_cases(golden_file)
    service = SkinAssistantService(enable_cart_drafts=False, embedder=LexicalOnlyEmbedder())
    service.answer_chain._chain = None
    documents = service.knowledge_base.list_documents()
    allowed_ids = {item.document_id for item in documents if item.approved and item.on_sale}
    globally_blocked_ids = {item.document_id for item in documents if not item.approved or not item.on_sale}

    records: list[dict[str, Any]] = []
    recall_at_5_total = 0.0
    recall_at_3_total = 0.0
    reciprocal_rank_total = 0.0
    first_hit_at_3_count = 0
    rank_4_5_rescue_count = 0
    citation_count = 0
    correct_citation_count = 0
    retrieval_count = 0
    violation_count = 0
    violating_case_count = 0

    for case in cases:
        expected_ids = set(case["expected_evidence_ids"])
        if not expected_ids:
            continue
        unknown_ids = expected_ids - allowed_ids
        if unknown_ids:
            raise ValueError(f"{case['id']} 的预期证据不在审核且在售资料中：{sorted(unknown_ids)}")
        forbidden_ids = globally_blocked_ids | set(case["must_not_retrieve_ids"])
        ranked = _unique_ids(service.knowledge_base.search(case["prompt"], [], limit=5))
        top3 = ranked[:3]
        relevant_at_5 = expected_ids.intersection(ranked)
        relevant_at_3 = expected_ids.intersection(top3)
        first_rank = next((index + 1 for index, item in enumerate(ranked) if item in expected_ids), None)
        recalled_violations = [item for item in ranked if item in forbidden_ids or item not in allowed_ids]

        result = service.build_result(case["prompt"], f"retrieval_eval_{case['id']}")
        citation_ids = _unique_ids(result.evidence)
        incorrect_citations = [item for item in citation_ids if item not in expected_ids]
        citation_violations = [item for item in citation_ids if item in forbidden_ids or item not in allowed_ids]
        case_has_violation = bool(recalled_violations or citation_violations)

        recall_at_5_total += len(relevant_at_5) / len(expected_ids)
        recall_at_3_total += len(relevant_at_3) / len(expected_ids)
        reciprocal_rank_total += 1 / first_rank if first_rank else 0
        first_hit_at_3_count += int(first_rank is not None and first_rank <= 3)
        rank_4_5_rescue_count += int(first_rank is not None and 4 <= first_rank <= 5)
        citation_count += len(citation_ids)
        correct_citation_count += sum(item in expected_ids for item in citation_ids)
        retrieval_count += len(ranked)
        violation_count += len(recalled_violations)
        violating_case_count += int(case_has_violation)
        records.append(
            {
                "case_id": case["id"],
                "question": case["prompt"],
                "expected_evidence_ids": sorted(expected_ids),
                "must_not_retrieve_ids": sorted(forbidden_ids),
                "ranked_evidence_ids_top5": ranked,
                "agent_citation_ids_top3": citation_ids,
                "recall_at_5": round(len(relevant_at_5) / len(expected_ids), 4),
                "recall_at_3": round(len(relevant_at_3) / len(expected_ids), 4),
                "reciprocal_rank": round(1 / first_rank, 4) if first_rank else 0.0,
                "first_relevant_rank": first_rank,
                "correct_citation_ids": sorted(set(citation_ids).intersection(expected_ids)),
                "incorrect_citation_ids": incorrect_citations,
                "recalled_violation_ids": recalled_violations,
                "citation_violation_ids": citation_violations,
            }
        )

    evaluated_cases = len(records)
    if not evaluated_cases:
        raise ValueError("黄金集没有需要证据的题目，无法计算检索指标")
    metrics = {
        "recall_at_5": round(recall_at_5_total / evaluated_cases, 4),
        "mrr": round(reciprocal_rank_total / evaluated_cases, 4),
        "recall_at_3": round(recall_at_3_total / evaluated_cases, 4),
        "first_hit_at_3": _ratio(first_hit_at_3_count, evaluated_cases),
        "rank_4_5_rescue_rate": _ratio(rank_4_5_rescue_count, evaluated_cases),
        "citation_correctness": _ratio(correct_citation_count, citation_count),
        "violation_retrieval_rate": _ratio(violation_count, retrieval_count),
        "violation_case_rate": _ratio(violating_case_count, evaluated_cases),
    }
    return {
        "report_version": "retrieval-baseline-1",
        "generated_at": datetime.now(UTC).isoformat(),
        "execution_mode": "deterministic_local_no_external_model_or_transaction_write",
        "scope": {
            "golden_cases": len(cases),
            "evaluated_evidence_cases": evaluated_cases,
            "total_document_count": len(documents),
            "searchable_document_count": len(allowed_ids),
            "globally_blocked_document_ids": sorted(globally_blocked_ids),
            "ranking_scope": "审核且在售的全库文档级 Top 5；不混入候选 SKU 业务过滤。",
            "citation_scope": "实际 Agent 工作流的 Top 3 引用，保留候选 SKU、审核和在售过滤。",
        },
        "metrics": metrics,
        "reranker_gate": {
            "corpus_window": [MIN_RERANKER_CORPUS_SIZE, MAX_RERANKER_CORPUS_SIZE],
            "min_recall_at_5": MIN_RECALL_AT_5_FOR_RERANKING,
            "min_first_hit_at_3": MIN_FIRST_HIT_AT_3,
            "min_recall_at_3": MIN_RECALL_AT_3,
            **_reranker_decision(
                len(allowed_ids), metrics, metrics["violation_retrieval_rate"]
            ),
        },
        "cases": records,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """渲染可给内容审核、检索和业务共同复核的紧凑报告。"""
    scope = report["scope"]
    metrics = report["metrics"]
    gate = report["reranker_gate"]
    lines = [
        "# 检索基线与 Reranker 准入报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 执行模式：`{report['execution_mode']}`",
        (
            f"- 语料：共 {scope['total_document_count']} 份，其中审核且在售 "
            f"{scope['searchable_document_count']} 份；全局过滤资料："
            f"{', '.join(scope['globally_blocked_document_ids']) or '无'}。"
        ),
        f"- 评测范围：{scope['evaluated_evidence_cases']}/{scope['golden_cases']} 条需证据黄金题。",
        f"- 排序范围：{scope['ranking_scope']}",
        f"- 引用范围：{scope['citation_scope']}",
        "",
        "## 指标",
        "",
        "| 指标 | 值 |",
        "| --- | ---: |",
        f"| Recall@5 | {metrics['recall_at_5']:.2%} |",
        f"| MRR | {metrics['mrr']:.4f} |",
        f"| Recall@3 | {metrics['recall_at_3']:.2%} |",
        f"| Top 3 首个正确结果命中率 | {metrics['first_hit_at_3']:.2%} |",
        f"| 第 4–5 位可被重排挽回率 | {metrics['rank_4_5_rescue_rate']:.2%} |",
        f"| 引用正确率 | {metrics['citation_correctness']:.2%} |",
        f"| 违规召回率 | {metrics['violation_retrieval_rate']:.2%} |",
        f"| 违规题目占比 | {metrics['violation_case_rate']:.2%} |",
        "",
        "## Reranker 决策",
        "",
        f"- 结论：**{'建议接入' if gate['should_add_reranker'] else '暂不接入'}**（`{gate['decision']}`）",
        f"- 原因：{gate['reason']}",
        (
            f"- 门槛：审核且在售语料 {gate['corpus_window'][0]}–{gate['corpus_window'][1]} 份；"
            f"Recall@5 ≥ {gate['min_recall_at_5']:.0%}；Top 3 首个正确结果命中率 ≥ "
            f"{gate['min_first_hit_at_3']:.0%}；Recall@3 ≥ {gate['min_recall_at_3']:.0%}。"
        ),
        "",
        "## 逐题检索明细",
        "",
        "| 题目 | 预期证据 | Top 5 | 实际引用 Top 3 | R@5 | RR | 违规 |",
        "| --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for case in report["cases"]:
        violations = case["recalled_violation_ids"] + case["citation_violation_ids"]
        lines.append(
            "| {case_id} | {expected} | {ranked} | {citations} | {recall:.2%} | {rr:.4f} | {violations} |".format(
                case_id=case["case_id"],
                expected="、".join(case["expected_evidence_ids"]),
                ranked="、".join(case["ranked_evidence_ids_top5"]) or "无",
                citations="、".join(case["agent_citation_ids_top3"]) or "无",
                recall=case["recall_at_5"],
                rr=case["reciprocal_rank"],
                violations="、".join(violations) or "无",
            )
        )
    return "\n".join(lines) + "\n"


def export_report(report: dict[str, Any], output: Path, report_format: ReportFormat) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if report_format == "json"
        else render_markdown(report)
    )
    output.write_text(content, encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="执行黄金题检索基线并判断是否应接入 Reranker")
    parser.add_argument("--golden-file", type=Path, default=DEFAULT_GOLDEN_FILE)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    args = parser.parse_args()
    report = build_retrieval_report(args.golden_file)
    output = export_report(report, args.output, args.format)
    gate = report["reranker_gate"]
    print(f"已导出检索报告：{output}；Reranker：{gate['decision']}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
