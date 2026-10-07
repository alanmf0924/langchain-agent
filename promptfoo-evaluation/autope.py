"""AutoPE 控制面：DSPy 产候选，Promptfoo 判候选，Langfuse 回流待审核考题。

本文件只管理评测工件，不会写入业务默认 Prompt、黄金集或 Langfuse 线上数据。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_BRIEF = ROOT / "autope" / "brief.json"
DEFAULT_REVIEWED_CASES = ROOT / "autope" / "reviewed_cases.json"
REQUIRED_CASE_FIELDS = (
    "id",
    "dimension",
    "prompt",
    "expected_intent",
    "expected_action",
    "must_have_evidence",
    "expected_evidence_ids",
    "must_not_retrieve_ids",
    "source",
)
PHONE_PATTERN = re.compile(r"(?<!\d)1\d{10}(?!\d)")
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


class AutoPEError(ValueError):
    """输入工件不满足可审计或安全契约。"""


def _json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AutoPEError(f"找不到工件：{path}") from error
    except json.JSONDecodeError as error:
        raise AutoPEError(f"工件不是合法 JSON：{path} ({error.msg})") from error


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso8601(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise AutoPEError(f"{name} 必须是 ISO 8601 时间") from error
    if parsed.tzinfo is None:
        raise AutoPEError(f"{name} 必须含时区，例如 2026-10-07T00:00:00Z")
    return parsed.astimezone(UTC)


def _baseline_system_prompt() -> str:
    """从当前业务源读取基线 Prompt，避免在评测工程复制一份可漂移文本。"""
    project_root = ROOT.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    try:
        from app.answer_chain import SYSTEM_PROMPT
    except ImportError as error:
        raise AutoPEError("无法读取业务 SYSTEM_PROMPT；请从业务仓库内运行") from error
    return SYSTEM_PROMPT


def _brief(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict) or value.get("schema_version") != "autope-brief-v1":
        raise AutoPEError("brief 必须是 schema_version=autope-brief-v1 的对象")
    if not isinstance(value.get("goal"), str) or not value["goal"].strip():
        raise AutoPEError("brief.goal 不能为空")
    clauses = value.get("required_clauses")
    if not isinstance(clauses, list) or not clauses or not all(isinstance(item, str) and item for item in clauses):
        raise AutoPEError("brief.required_clauses 必须是非空字符串数组")
    count = value.get("candidate_count")
    if not isinstance(count, int) or not 1 <= count <= 8:
        raise AutoPEError("brief.candidate_count 必须在 1 到 8 之间")
    case_ids = value.get("training_case_ids", [])
    if not isinstance(case_ids, list) or not all(isinstance(item, str) for item in case_ids):
        raise AutoPEError("brief.training_case_ids 必须是字符串数组")
    return value


def _parse_candidate_response(raw: str) -> list[dict[str, str]]:
    """DSPy 输出必须是 JSON，拒绝从自然语言猜测候选内容。"""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise AutoPEError(f"DSPy 未返回合法 JSON：{error.msg}") from error
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or not candidates:
        raise AutoPEError("DSPy 输出必须包含非空 candidates 数组")
    normalized: list[dict[str, str]] = []
    for item in candidates:
        if not isinstance(item, dict):
            raise AutoPEError("DSPy candidate 必须是对象")
        prompt = item.get("system_prompt")
        hypothesis = item.get("hypothesis")
        if not isinstance(prompt, str) or not prompt.strip():
            raise AutoPEError("DSPy candidate.system_prompt 不能为空")
        if not isinstance(hypothesis, str) or not hypothesis.strip():
            raise AutoPEError("DSPy candidate.hypothesis 不能为空")
        normalized.append({"system_prompt": prompt.strip(), "hypothesis": hypothesis.strip()})
    return normalized


def build_candidate_manifest(
    brief: dict[str, Any], raw_candidates: Iterable[dict[str, str]], baseline_prompt: str | None = None
) -> dict[str, Any]:
    """为 DSPy 候选加上不可变来源与安全条款证明。"""
    baseline = baseline_prompt or _baseline_system_prompt()
    normalized: list[dict[str, Any]] = []
    seen_prompts: set[str] = set()
    for item in raw_candidates:
        prompt = item["system_prompt"].strip()
        if prompt == baseline:
            raise AutoPEError("候选与基线 Prompt 完全相同，不应进入候选池")
        missing = [clause for clause in brief["required_clauses"] if clause not in prompt]
        if missing:
            raise AutoPEError(f"候选删除了不可放宽条款：{missing[0]}")
        prompt_hash = _text_hash(prompt)
        if prompt_hash in seen_prompts:
            continue
        seen_prompts.add(prompt_hash)
        candidate_id = f"dspy_{prompt_hash[:16]}"
        normalized.append(
            {
                "candidate_id": candidate_id,
                "generator": "dspy",
                "hypothesis": item["hypothesis"].strip(),
                "system_prompt": prompt,
                "system_prompt_sha256": prompt_hash,
                "baseline_prompt_sha256": _text_hash(baseline),
                "required_clauses_sha256": _json_hash(brief["required_clauses"]),
                "training_case_ids": brief["training_case_ids"],
            }
        )
    if not normalized:
        raise AutoPEError("没有可用的非重复候选")
    if len(normalized) > brief["candidate_count"]:
        raise AutoPEError("DSPy 输出候选数超过 brief.candidate_count")
    return {
        "schema_version": "autope-candidates-v1",
        "created_at": _utc_now(),
        "brief_sha256": _json_hash(brief),
        "baseline_prompt_sha256": _text_hash(baseline),
        "candidates": normalized,
    }


def _dspy_response(brief: dict[str, Any], baseline_prompt: str) -> str:
    """调用 DSPy 生成候选；模型与密钥只从运行时环境读取，绝不写入工件。"""
    try:
        import dspy
    except ImportError as error:
        raise AutoPEError("未安装 dspy；请在隔离评测环境安装 dspy 后重试") from error
    model = os.getenv("AUTOPE_DSPY_MODEL")
    if not model:
        raise AutoPEError("缺少 AUTOPE_DSPY_MODEL，例如 openai/gpt-4.1-mini")
    lm_kwargs: dict[str, str] = {"api_key": os.getenv("AUTOPE_DSPY_API_KEY", os.getenv("OPENAI_API_KEY", ""))}
    if base_url := os.getenv("AUTOPE_DSPY_BASE_URL"):
        lm_kwargs["api_base"] = base_url
    dspy.configure(lm=dspy.LM(model, **lm_kwargs))
    generator = dspy.Predict("brief, baseline_prompt -> candidate_json")
    instruction = {
        "goal": brief["goal"],
        "candidate_count": brief["candidate_count"],
        "required_clauses": brief["required_clauses"],
        "training_case_ids": brief["training_case_ids"],
        "output_contract": {
            "format": "JSON only",
            "schema": {"candidates": [{"system_prompt": "...", "hypothesis": "..."}]},
            "rule": "Every system_prompt must retain every required_clauses item verbatim.",
        },
    }
    response = generator(brief=json.dumps(instruction, ensure_ascii=False), baseline_prompt=baseline_prompt)
    value = getattr(response, "candidate_json", None)
    if not isinstance(value, str):
        raise AutoPEError("DSPy 未返回 candidate_json 字符串")
    return value


def generate_dspy_candidates(brief_path: Path, output_path: Path) -> dict[str, Any]:
    brief = _brief(brief_path)
    baseline = _baseline_system_prompt()
    raw = _parse_candidate_response(_dspy_response(brief, baseline))
    manifest = build_candidate_manifest(brief, raw, baseline)
    _write_json(output_path, manifest)
    return manifest


def load_candidate_file(path: Path) -> dict[str, str]:
    """加载一个候选；多候选 manifest 必须以环境变量精确选择，不能默认取第一条。"""
    value = _read_json(path)
    if isinstance(value, dict) and value.get("schema_version") == "autope-candidates-v1":
        candidates = value.get("candidates")
        wanted = os.getenv("AUTOPE_CANDIDATE_ID")
        if not isinstance(candidates, list) or not wanted:
            raise AutoPEError("候选 manifest 必须设置 AUTOPE_CANDIDATE_ID")
        selected = next((item for item in candidates if item.get("candidate_id") == wanted), None)
        if selected is None:
            raise AutoPEError(f"候选 manifest 中不存在 {wanted}")
    elif isinstance(value, dict):
        selected = value
    else:
        raise AutoPEError("候选文件必须是对象或 autope-candidates-v1 manifest")
    required = ("candidate_id", "system_prompt", "system_prompt_sha256", "baseline_prompt_sha256")
    if not all(isinstance(selected.get(key), str) and selected[key] for key in required):
        raise AutoPEError("候选缺少必填字段")
    if _text_hash(selected["system_prompt"]) != selected["system_prompt_sha256"]:
        raise AutoPEError("候选 system_prompt_sha256 不匹配")
    return {key: selected[key] for key in required}


def _base_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise AutoPEError("LANGFUSE_BASE_URL 必须是完整 HTTP(S) URL")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise AutoPEError("Langfuse 非本地服务必须使用 HTTPS")
    return value.rstrip("/")


def fetch_langfuse_observations(
    from_time: str, to_time: str, output_path: Path, limit: int = 100
) -> dict[str, Any]:
    """按有界时间窗读取 Langfuse v2 observations，并保留原始导出在本地忽略目录。"""
    start = _parse_iso8601(from_time, "from")
    end = _parse_iso8601(to_time, "to")
    if not start < end or (end - start).days > 7:
        raise AutoPEError("Langfuse 拉取时间窗必须大于 0 且不超过 7 天")
    if not 1 <= limit <= 1000:
        raise AutoPEError("limit 必须在 1 到 1000 之间")
    base_url = _base_url(os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"))
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        raise AutoPEError("缺少 LANGFUSE_PUBLIC_KEY 或 LANGFUSE_SECRET_KEY")
    authorization = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    observations: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(observations) < limit:
        query = {
            "fromStartTime": start.isoformat().replace("+00:00", "Z"),
            "toStartTime": end.isoformat().replace("+00:00", "Z"),
            "type": "GENERATION",
            "fields": "core,basic,io,metadata,usage,metrics,trace_context",
            "limit": str(min(1000, limit - len(observations))),
        }
        if cursor:
            query["cursor"] = cursor
        url = f"{base_url}/api/public/v2/observations?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={"Authorization": f"Basic {authorization}"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except OSError as error:
            raise AutoPEError(f"Langfuse observations 拉取失败：{error}") from error
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise AutoPEError("Langfuse v2 返回缺少 data 数组")
        observations.extend(rows)
        meta = payload.get("meta", {})
        cursor = meta.get("cursor") if isinstance(meta, dict) else None
        if not cursor or not rows:
            break
    export = {
        "schema_version": "langfuse-observations-v2-export",
        "fetched_at": _utc_now(),
        "source_base_url": base_url,
        "from": from_time,
        "to": to_time,
        "observations": observations,
    }
    _write_json(output_path, export)
    return export


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _review_reasons(observation: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if observation.get("level") == "ERROR":
        reasons.append("observation_error")
    if observation.get("statusMessage"):
        reasons.append("status_message")
    metadata = _metadata(observation.get("metadata"))
    if metadata.get("autope_review") is True:
        reasons.append("explicit_review_flag")
    for key in ("user_feedback_score", "autope_score", "human_score"):
        value = metadata.get(key)
        if isinstance(value, (int, float)) and value <= 0:
            reasons.append(f"low_{key}")
    return reasons


def build_review_queue(export_path: Path, output_path: Path) -> dict[str, Any]:
    """把线上低质量/故障观测转成不含原文的人工审核队列。"""
    export = _read_json(export_path)
    if not isinstance(export, dict) or export.get("schema_version") != "langfuse-observations-v2-export":
        raise AutoPEError("输入必须是 langfuse-observations-v2-export")
    observations = export.get("observations")
    if not isinstance(observations, list):
        raise AutoPEError("Langfuse 导出缺少 observations 数组")
    pending: list[dict[str, Any]] = []
    for row in observations:
        if not isinstance(row, dict):
            continue
        reasons = _review_reasons(row)
        if not reasons:
            continue
        raw_input = row.get("input", "")
        raw_output = row.get("output", "")
        input_text = raw_input if isinstance(raw_input, str) else json.dumps(raw_input, ensure_ascii=False)
        output_text = raw_output if isinstance(raw_output, str) else json.dumps(raw_output, ensure_ascii=False)
        observation_id = row.get("id")
        trace_id = row.get("traceId")
        if not isinstance(observation_id, str) or not isinstance(trace_id, str):
            continue
        pending.append(
            {
                "review_id": f"review_{_text_hash(observation_id)[:16]}",
                "review_status": "pending_human_redaction_and_contract",
                "source": {
                    "langfuse_observation_id": observation_id,
                    "langfuse_trace_id": trace_id,
                    "observed_at": row.get("startTime"),
                    "source_base_url": export.get("source_base_url"),
                },
                "selection_reasons": reasons,
                # 不把线上原始问答复制进 Git 或长期评测集；审核人只通过 Langfuse 定位原记录。
                "input_sha256": _text_hash(input_text),
                "input_length": len(input_text),
                "output_sha256": _text_hash(output_text),
                "output_length": len(output_text),
            }
        )
    queue = {
        "schema_version": "autope-review-queue-v1",
        "created_at": _utc_now(),
        "source_export_sha256": _json_hash(export),
        "items": pending,
    }
    _write_json(output_path, queue)
    return queue


def _contains_obvious_pii(text: str) -> bool:
    return bool(PHONE_PATTERN.search(text) or EMAIL_PATTERN.search(text))


def load_approved_cases(path: Path | None = None) -> list[dict[str, Any]]:
    """只加载人工完成脱敏、标注并审批的线上题；所有其他状态一律拒绝。"""
    candidate_path = path or Path(os.getenv("AUTOPE_APPROVED_CASES", DEFAULT_REVIEWED_CASES))
    rows = _read_json(candidate_path)
    if not isinstance(rows, list):
        raise AutoPEError("reviewed_cases 必须是 JSON 数组")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise AutoPEError("reviewed_cases 每项必须是对象")
        if row.get("review_status") != "approved":
            raise AutoPEError("reviewed_cases 只允许 review_status=approved 的记录")
        missing = [field for field in REQUIRED_CASE_FIELDS if field not in row]
        if missing:
            raise AutoPEError(f"审核题缺少字段：{missing[0]}")
        case_id = row["id"]
        prompt = row["prompt"]
        if not isinstance(case_id, str) or not case_id.startswith("online-") or case_id in seen_ids:
            raise AutoPEError("线上题 id 必须以 online- 开头且唯一")
        if not isinstance(prompt, str) or not 2 <= len(prompt) <= 500:
            raise AutoPEError(f"线上题 {case_id} 的 prompt 长度无效")
        if _contains_obvious_pii(prompt):
            raise AutoPEError(f"线上题 {case_id} 疑似仍含手机号或邮箱")
        if not isinstance(row["expected_evidence_ids"], list) or not isinstance(
            row["must_not_retrieve_ids"], list
        ):
            raise AutoPEError(f"线上题 {case_id} 的证据字段必须是数组")
        if not isinstance(row["must_have_evidence"], bool):
            raise AutoPEError(f"线上题 {case_id} 的 must_have_evidence 必须是布尔值")
        source = row["source"]
        if not isinstance(source, dict) or not all(
            isinstance(source.get(key), str) and source[key]
            for key in ("langfuse_trace_id", "langfuse_observation_id")
        ):
            raise AutoPEError(f"线上题 {case_id} 必须保留 Langfuse trace/observation 定位")
        for key in ("dimension", "expected_intent", "expected_action"):
            if not isinstance(row[key], str) or not row[key]:
                raise AutoPEError(f"线上题 {case_id} 的 {key} 无效")
        normalized_case = dict(row)
        normalized_case["evaluation_mode"] = row.get("evaluation_mode", "read_only")
        if normalized_case["evaluation_mode"] not in {"read_only", "confirmation_preview"}:
            raise AutoPEError(f"线上题 {case_id} 的 evaluation_mode 无效")
        normalized_case["expected_evidence_ids_json"] = json.dumps(
            row["expected_evidence_ids"], ensure_ascii=False
        )
        normalized_case["must_not_retrieve_ids_json"] = json.dumps(
            row["must_not_retrieve_ids"], ensure_ascii=False
        )
        normalized.append(normalized_case)
        seen_ids.add(case_id)
    return normalized


def _promptfoo_summary(path: Path) -> dict[str, Any]:
    report = _read_json(path)
    try:
        rows = report["results"]["results"]
    except (KeyError, TypeError) as error:
        raise AutoPEError("Promptfoo 结果缺少 results.results") from error
    if not isinstance(rows, list) or not rows:
        raise AutoPEError("Promptfoo 结果没有逐题记录")
    all_case_ids: set[str] = set()
    pass_case_ids: set[str] = set()
    candidate_ids: set[str] = set()
    candidates_applied = 0
    cost = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        variables = row.get("vars", {})
        case_id = variables.get("case_id") if isinstance(variables, dict) else None
        if isinstance(case_id, str):
            all_case_ids.add(case_id)
            if row.get("success") is True:
                pass_case_ids.add(case_id)
        row_cost = row.get("cost")
        if isinstance(row_cost, (int, float)):
            cost += float(row_cost)
        response = row.get("response", {})
        output = response.get("output") if isinstance(response, dict) else None
        if isinstance(output, str):
            try:
                payload = json.loads(output)
            except json.JSONDecodeError:
                continue
            candidate = payload.get("candidate", {})
            if isinstance(candidate, dict):
                candidate_id = candidate.get("candidate_id")
                if isinstance(candidate_id, str):
                    candidate_ids.add(candidate_id)
                if candidate.get("applied") is True:
                    candidates_applied += 1
    return {
        "case_ids": sorted(all_case_ids),
        "passed_case_ids": sorted(pass_case_ids),
        "candidate_ids": sorted(candidate_ids),
        "candidate_applied_count": candidates_applied,
        "row_count": len(rows),
        "cost": round(cost, 8),
    }


def select_candidate(
    baseline_path: Path, candidate_result_path: Path, output_path: Path, max_cost: float
) -> dict[str, Any]:
    """Promptfoo 只给候选“人工发布复核资格”，从不自动发布到业务 Prompt。"""
    baseline = _promptfoo_summary(baseline_path)
    candidate = _promptfoo_summary(candidate_result_path)
    reasons: list[str] = []
    if set(baseline["case_ids"]) != set(baseline["passed_case_ids"]):
        reasons.append("基线结果自身未全量通过，拒绝作为比较基线")
    if len(candidate["candidate_ids"]) != 1:
        reasons.append("候选结果必须且只能包含一个 candidate_id")
    if candidate["candidate_applied_count"] != candidate["row_count"]:
        reasons.append("存在未实际应用候选 Prompt 的评测行")
    if set(candidate["case_ids"]) != set(candidate["passed_case_ids"]):
        reasons.append("候选未通过全部 Promptfoo 硬门禁")
    if not set(baseline["case_ids"]).issubset(set(candidate["passed_case_ids"])):
        reasons.append("候选未完整覆盖并通过固定黄金集")
    if candidate["cost"] > max_cost:
        reasons.append(f"候选评测成本 {candidate['cost']} 超过上限 {max_cost}")
    report = {
        "schema_version": "autope-promotion-report-v1",
        "created_at": _utc_now(),
        "baseline_results_sha256": _json_hash(_read_json(baseline_path)),
        "candidate_results_sha256": _json_hash(_read_json(candidate_result_path)),
        "candidate_id": candidate["candidate_ids"][0] if len(candidate["candidate_ids"]) == 1 else None,
        "decision": "eligible_for_human_release_review" if not reasons else "rejected",
        "reasons": reasons or ["全部硬门禁通过；仍须人工发布复核，未自动更新业务 Prompt。"],
        "baseline": baseline,
        "candidate": candidate,
        "max_cost": max_cost,
    }
    _write_json(output_path, report)
    return report


def _command_dspy_generate(args: argparse.Namespace) -> int:
    manifest = generate_dspy_candidates(Path(args.brief), Path(args.output))
    print(json.dumps({"candidates": len(manifest["candidates"]), "output": args.output}, ensure_ascii=False))
    return 0


def _command_langfuse_fetch(args: argparse.Namespace) -> int:
    export = fetch_langfuse_observations(args.from_time, args.to_time, Path(args.output), args.limit)
    print(json.dumps({"observations": len(export["observations"]), "output": args.output}, ensure_ascii=False))
    return 0


def _command_build_review_queue(args: argparse.Namespace) -> int:
    queue = build_review_queue(Path(args.input), Path(args.output))
    print(json.dumps({"pending_reviews": len(queue["items"]), "output": args.output}, ensure_ascii=False))
    return 0


def _command_validate_reviewed(args: argparse.Namespace) -> int:
    cases = load_approved_cases(Path(args.input))
    print(json.dumps({"approved_cases": len(cases), "input": args.input}, ensure_ascii=False))
    return 0


def _command_select(args: argparse.Namespace) -> int:
    report = select_candidate(Path(args.baseline), Path(args.candidate_result), Path(args.output), args.max_cost)
    print(json.dumps({"decision": report["decision"], "output": args.output}, ensure_ascii=False))
    return 0 if report["decision"] == "eligible_for_human_release_review" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AutoPE 控制面")
    commands = parser.add_subparsers(dest="command", required=True)
    dspy_generate = commands.add_parser("dspy-generate", help="使用 DSPy 生成受约束 Prompt 候选")
    dspy_generate.add_argument("--brief", default=str(DEFAULT_BRIEF))
    dspy_generate.add_argument("--output", required=True)
    dspy_generate.set_defaults(handler=_command_dspy_generate)
    fetch = commands.add_parser("langfuse-fetch", help="拉取有界时间窗的 Langfuse v2 observations")
    fetch.add_argument("--from", dest="from_time", required=True)
    fetch.add_argument("--to", dest="to_time", required=True)
    fetch.add_argument("--output", required=True)
    fetch.add_argument("--limit", type=int, default=100)
    fetch.set_defaults(handler=_command_langfuse_fetch)
    queue = commands.add_parser("build-review-queue", help="把线上异常信号转为脱敏审核队列")
    queue.add_argument("--input", required=True)
    queue.add_argument("--output", required=True)
    queue.set_defaults(handler=_command_build_review_queue)
    validate = commands.add_parser("validate-reviewed", help="校验人工批准的线上回流题")
    validate.add_argument("--input", default=str(DEFAULT_REVIEWED_CASES))
    validate.set_defaults(handler=_command_validate_reviewed)
    select = commands.add_parser("select-candidate", help="生成仅供人工发布复核的候选晋级报告")
    select.add_argument("--baseline", required=True)
    select.add_argument("--candidate-result", required=True)
    select.add_argument("--output", required=True)
    select.add_argument("--max-cost", type=float, default=2.0)
    select.set_defaults(handler=_command_select)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except AutoPEError as error:
        parser.exit(2, f"AutoPE 输入拒绝：{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
