"""生成可交付业务评审的 V1 黄金问题评估报告。

本模块复用线上同一条 ``SkinAssistantService.build_result`` 链路，但强制关闭
回答链中的外部模型，并显式关闭交易草稿。因此报告可以在没有任何密钥的环境中
稳定复跑，也不会因为评估而生成购物车草稿。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.embeddings import LexicalOnlyEmbedder
from app.service import SkinAssistantService

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDEN_FILE = ROOT / "eval" / "golden_questions.json"
DEFAULT_OUTPUT_FILE = ROOT / "eval" / "reports" / "v1_golden_report.md"
ReportFormat = Literal["markdown", "json"]


def _evidence_summary(evidence: list[Any]) -> list[dict[str, str]]:
    """报告只保留业务复核所需的证据标识与版本，不重复暴露整段资料。"""
    return [
        {
            "id": item.id,
            "version": item.version,
            "sku_id": item.sku_id,
            "title": item.title,
        }
        for item in evidence
    ]


def _security_action(intent: str, fallback_reason: str | None) -> dict[str, Any]:
    """把安全路由显式写入评审产物，避免仅凭空证据让业务方猜测原因。"""
    if intent == "safety":
        return {
            "action": "stop_recommendation",
            "enforced": True,
            "reason": fallback_reason or "safety_policy",
            "external_model_called": False,
        }
    return {
        "action": "none",
        "enforced": False,
        "reason": None,
        "external_model_called": False,
    }


def _transaction_action(result: Any) -> dict[str, Any]:
    """记录问答阶段是否仅提议交易，而不是把提议误读为已执行。"""
    return {
        "cart_drafts_enabled_for_evaluation": False,
        "requires_confirmation": result.requires_confirmation,
        "proposed_actions": result.actions,
        "executed": False,
    }


def build_v1_golden_report(golden_file: Path = DEFAULT_GOLDEN_FILE) -> dict[str, Any]:
    """逐题执行 V1 基础问答链路并返回可 JSON 序列化的评估结果。

    不抛出单题业务断言失败，让评审报告仍能完整呈现全部黄金题的实际输出；
    是否符合黄金集预期由每题 ``checks`` 和顶层汇总共同表达。
    """
    cases = json.loads(golden_file.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise TypeError("黄金问题文件必须是 JSON 数组")

    # 显式传 False，避免本机环境变量意外开启交易草稿功能。
    service = SkinAssistantService(
        enable_cart_drafts=False,
        # 不读取 RAG_EMBEDDER 的环境配置，阻断远程 Embeddings API。
        embedder=LexicalOnlyEmbedder(),
    )
    # 即使 .env 配置了 OpenAI 兼容服务，也绝不在评估中发起模型请求。
    service.answer_chain._chain = None
    records: list[dict[str, Any]] = []

    for case in cases:
        audit_start = len(service.audit_logs)
        usage_start = len(service.model_usage_logs)
        result = service.build_result(case["prompt"], f"eval_{case['id']}")
        evidence = _evidence_summary(result.evidence)
        checks = {
            "intent_matches_expected": result.query_plan.intent == case["expected_intent"],
            "next_action_matches_expected": result.query_plan.next_action == case["expected_action"],
            "evidence_matches_expected": bool(evidence) is case["must_have_evidence"],
        }
        checks["passed"] = all(checks.values())
        # 只截取本题新增的审计，防止前题记录混入当前问题的复核材料。
        tool_audit = [
            item.model_dump(mode="json") for item in service.audit_logs[audit_start:]
        ]
        model_usage = [
            item.model_dump(mode="json") for item in service.model_usage_logs[usage_start:]
        ]
        records.append(
            {
                "case_id": case["id"],
                "dimension": case["dimension"],
                "question": case["prompt"],
                "expected": {
                    "intent": case["expected_intent"],
                    "next_action": case["expected_action"],
                    "must_have_evidence": case["must_have_evidence"],
                },
                "query_plan": result.query_plan.model_dump(mode="json"),
                "answer": result.answer,
                "evidence": evidence,
                "recommended_bundles": [
                    {"bundle_id": bundle.bundle_id, "name": bundle.name}
                    for bundle in result.bundles
                ],
                "tool_audit": tool_audit,
                "security_action": _security_action(
                    result.query_plan.intent, result.model_usage.skipped_or_fallback_reason
                ),
                "transaction_action": _transaction_action(result),
                "model_usage": model_usage,
                "checks": checks,
            }
        )

    dimensions = Counter(record["dimension"] for record in records)
    passed_count = sum(record["checks"]["passed"] for record in records)
    tool_audit_count = sum(len(record["tool_audit"]) for record in records)
    return {
        "report_version": "v1-golden-evaluation-1",
        "generated_at": datetime.now(UTC).isoformat(),
        "execution_mode": {
            "name": "deterministic_local_no_external_model",
            "external_model_calls": 0,
            "cart_drafts_enabled": False,
            "transaction_writes_executed": 0,
        },
        "summary": {
            "total_cases": len(records),
            "passed_cases": passed_count,
            "failed_cases": len(records) - passed_count,
            "dimensions": dict(sorted(dimensions.items())),
            "tool_audit_entries": tool_audit_count,
            "safety_stops": sum(
                record["security_action"]["action"] == "stop_recommendation"
                for record in records
            ),
        },
        "cases": records,
    }


def _inline_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ": "))


def render_markdown(report: dict[str, Any]) -> str:
    """把完整结构化结果渲染成适合业务评审阅读的 Markdown。"""
    summary = report["summary"]
    lines = [
        "# 澄肌护肤选购助手 V1 黄金问题评估报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 执行模式：`{report['execution_mode']['name']}`",
        f"- 结果：{summary['passed_cases']}/{summary['total_cases']} 条符合黄金集预期",
        f"- 工具审计：{summary['tool_audit_entries']} 条只读工具记录；安全停止：{summary['safety_stops']} 条",
        "- 安全与交易边界：本次强制禁用外部模型和购物车草稿；不会产生交易写入。",
        "",
        "## 汇总",
        "",
        "| 维度 | 题数 |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {dimension} | {count} |" for dimension, count in summary["dimensions"].items())
    lines.extend(["", "## 逐题明细", ""])

    for record in report["cases"]:
        status = "通过" if record["checks"]["passed"] else "未通过"
        evidence = (
            "；".join(f"{item['id']}（{item['version']}）" for item in record["evidence"])
            or "无"
        )
        bundles = "；".join(item["name"] for item in record["recommended_bundles"]) or "无"
        lines.extend(
            [
                f"### {record['case_id']} · {record['dimension']} · {status}",
                "",
                f"- 问题：{record['question']}",
                f"- 预期：intent=`{record['expected']['intent']}`，next_action=`{record['expected']['next_action']}`，证据={'需要' if record['expected']['must_have_evidence'] else '不需要'}",
                f"- QueryPlan：`{_inline_json(record['query_plan'])}`",
                f"- 回答：{record['answer']}",
                f"- 证据 ID / 版本：{evidence}",
                f"- 推荐套餐：{bundles}",
                f"- 工具审计：`{_inline_json(record['tool_audit'])}`",
                f"- 安全动作：`{_inline_json(record['security_action'])}`",
                f"- 交易动作：`{_inline_json(record['transaction_action'])}`",
                f"- 校验：`{_inline_json(record['checks'])}`",
                "",
            ]
        )
    return "\n".join(lines)


def export_report(report: dict[str, Any], output: Path, report_format: ReportFormat) -> Path:
    """导出 JSON 或 Markdown；只创建用户指定输出文件的父目录。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    if report_format == "json":
        content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    else:
        content = render_markdown(report) + "\n"
    output.write_text(content, encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="执行 V1 黄金问题并导出业务评审报告")
    parser.add_argument("--golden-file", type=Path, default=DEFAULT_GOLDEN_FILE)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    args = parser.parse_args()

    report = build_v1_golden_report(args.golden_file)
    output = export_report(report, args.output, args.format)
    summary = report["summary"]
    print(f"已导出 {summary['passed_cases']}/{summary['total_cases']} 条通过的评估报告：{output}")
    return 0 if summary["failed_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
