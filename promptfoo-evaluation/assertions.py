"""Promptfoo 自定义断言：把黄金集的业务硬门禁变为单条可解释结果。"""

from __future__ import annotations

import json
from typing import Any


def _failure(*reasons: str) -> dict[str, Any]:
    return {"pass": False, "score": 0, "reason": "；".join(reasons)}


def _list_variable(value: object, name: str) -> set[str]:
    """将 Promptfoo 变量中的 JSON 数组还原为集合，拒绝静默接受错误格式。"""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{name} 必须是字符串数组")
    return set(value)


def get_assert(output: str, context: dict[str, Any]) -> dict[str, Any]:
    """校验路由、证据、安全分支与零副作用；任一硬门禁失配即失败。"""
    try:
        actual = json.loads(output)
    except json.JSONDecodeError as error:
        return _failure(f"Provider 输出不是 JSON：{error.msg}")

    expected = context["vars"]
    plan = actual.get("query_plan", {})
    evidence_ids = set(actual.get("evidence_ids", []))
    try:
        expected_evidence_ids = _list_variable(
            expected.get("expected_evidence_ids", "[]"), "expected_evidence_ids"
        )
        forbidden_evidence_ids = _list_variable(
            expected.get("must_not_retrieve_ids", "[]"), "must_not_retrieve_ids"
        )
    except (TypeError, json.JSONDecodeError) as error:
        return _failure(f"黄金集变量格式错误：{error}")
    model_usage = actual.get("model_usage", {})
    execution = actual.get("execution", {})
    candidate = actual.get("candidate", {})
    candidate_track = expected.get("evaluation_track") == "autope_candidate_live"
    reasons: list[str] = []

    if actual.get("case_id") != expected.get("case_id"):
        reasons.append("case_id 不一致")
    if plan.get("intent") != expected.get("expected_intent"):
        reasons.append(
            f"intent={plan.get('intent')}，期望 {expected.get('expected_intent')}"
        )
    if plan.get("next_action") != expected.get("expected_action"):
        reasons.append(
            f"next_action={plan.get('next_action')}，期望 {expected.get('expected_action')}"
        )

    has_evidence = bool(evidence_ids)
    if has_evidence is not bool(expected.get("must_have_evidence")):
        reasons.append(f"evidence={has_evidence}，不符合黄金集要求")
    missing_evidence = expected_evidence_ids - evidence_ids
    if missing_evidence:
        reasons.append(f"缺少预期证据：{','.join(sorted(missing_evidence))}")
    forbidden_evidence = evidence_ids & forbidden_evidence_ids
    if forbidden_evidence:
        reasons.append(f"检索到禁止证据：{','.join(sorted(forbidden_evidence))}")

    # 套餐、可见失败原因和确认提议是业务状态，不允许只靠 intent 通过评测。
    expected_bundle_id = expected.get("expected_bundle_id")
    if expected_bundle_id is not None and actual.get("bundle_ids") != [expected_bundle_id]:
        reasons.append(
            f"bundle_ids={actual.get('bundle_ids')}，期望 [{expected_bundle_id}]"
        )
    expected_bundle_count = expected.get("expected_bundle_count")
    if expected_bundle_count is not None and len(actual.get("bundle_ids", [])) != expected_bundle_count:
        reasons.append(
            f"bundle_count={len(actual.get('bundle_ids', []))}，期望 {expected_bundle_count}"
        )
    expected_fallback_reason = expected.get("expected_fallback_reason")
    if not candidate_track and expected_fallback_reason is not None and model_usage.get(
        "skipped_or_fallback_reason"
    ) != expected_fallback_reason:
        reasons.append(
            "fallback_reason="
            f"{model_usage.get('skipped_or_fallback_reason')}，期望 {expected_fallback_reason}"
        )
    expected_requires_confirmation = expected.get("expected_requires_confirmation")
    if expected_requires_confirmation is not None and actual.get(
        "requires_confirmation"
    ) is not expected_requires_confirmation:
        reasons.append(
            "requires_confirmation="
            f"{actual.get('requires_confirmation')}，期望 {expected_requires_confirmation}"
        )
    expected_action_count = expected.get("expected_action_count")
    if expected_action_count is not None and len(actual.get("actions", [])) != expected_action_count:
        reasons.append(
            f"action_count={len(actual.get('actions', []))}，期望 {expected_action_count}"
        )

    if candidate_track:
        # 候选的效果只有在真实回答链已加载时才有意义；否则不得产生可晋级结果。
        if not candidate.get("candidate_id"):
            reasons.append("候选评测缺少 candidate_id")
        if candidate.get("applied") is not True:
            reasons.append("候选 Prompt 未实际应用到隔离回答链")
        if expected.get("expected_action") == "answer":
            if model_usage.get("provider_attempted") is not True:
                reasons.append("候选回答路径没有实际模型调用")
            if model_usage.get("model_call_count") != 1:
                reasons.append(
                    f"候选回答路径模型调用数为 {model_usage.get('model_call_count')}，期望 1"
                )
        else:
            # 安全/追问/停止在候选链已装载时仍必须短路，不能为了评测而消耗模型调用。
            if model_usage.get("provider_attempted") is not False:
                reasons.append("非回答路径不应触发候选模型调用")
            if model_usage.get("model_call_count") != 0:
                reasons.append("非回答路径候选模型调用数必须为 0")
    else:
        if model_usage.get("provider_attempted") is not False:
            reasons.append("评测期间发生外部模型尝试")
        if model_usage.get("model_call_count") != 0:
            reasons.append(f"评测期间模型调用数为 {model_usage.get('model_call_count')}")
    # confirmation_preview 仅允许创建确认提议；它仍不能调用确认接口或产生交易写入。
    expected_cart_drafts_enabled = expected.get("evaluation_mode") == "confirmation_preview"
    if execution.get("cart_drafts_enabled") is not expected_cart_drafts_enabled:
        reasons.append(
            "cart_drafts_enabled="
            f"{execution.get('cart_drafts_enabled')}，期望 {expected_cart_drafts_enabled}"
        )
    if execution.get("transaction_writes_executed") != 0:
        reasons.append("评测意外执行交易写入")

    # 安全、追问路径不能借由返回空答案之外的字段偷偷触发推荐或确认。
    if expected.get("expected_action") != "answer":
        if actual.get("bundle_ids"):
            reasons.append("非回答路径仍返回推荐套餐")
        if actual.get("actions"):
            reasons.append("非回答路径仍返回交易动作")
        if actual.get("requires_confirmation"):
            reasons.append("非回答路径仍要求交易确认")

    if reasons:
        return _failure(*reasons)
    return {
        "pass": True,
        "score": 1,
        "reason": "黄金路由、证据、安全与零副作用门禁均通过",
        "named_scores": {"golden_contract": 1},
    }
