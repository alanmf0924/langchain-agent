"""Promptfoo Provider：安全地调用业务服务的确定性评测路径。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 评测必须使用版本化的本地仿真数据；不能因开发机环境变量误接入数据库或商城读模型。
os.environ["CATALOG_SOURCE"] = "mock"
# GroundedAnswerChain 在非 test 环境会加载 .env；固定 test 可阻止它覆盖上述离线数据源。
os.environ["APP_ENV"] = "test"

from app.embeddings import LexicalOnlyEmbedder
from app.persistence import SqlAssistantRepository, metadata
from app.service import SkinAssistantService

_services: dict[tuple[bool, str | None], SkinAssistantService] = {}


def _live_candidate() -> dict[str, str] | None:
    """读取显式指定的 AutoPE 候选；默认评测不接触候选或远程模型。"""
    candidate_file = os.getenv("AUTOPE_CANDIDATE_FILE")
    live_enabled = os.getenv("AUTOPE_LIVE_EVAL", "false").lower() == "true"
    if not candidate_file:
        if live_enabled:
            raise ValueError("AUTOPE_LIVE_EVAL=true 时必须设置 AUTOPE_CANDIDATE_FILE")
        return None
    if not live_enabled:
        raise ValueError("候选 Prompt 只允许在 AUTOPE_LIVE_EVAL=true 的显式评测中加载")

    # autope.py 与 Provider 同目录，避免将候选定义复制到业务服务或静态配置中。
    from autope import load_candidate_file

    return load_candidate_file(Path(candidate_file))


def _configure_candidate_chain(service: SkinAssistantService, candidate: dict[str, str]) -> bool:
    """只在隔离 Provider 内把候选 Prompt 接到真实回答链，绝不修改线上默认 Prompt。"""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return False

    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI

    answer_chain = service.answer_chain
    model = ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "deepseek-chat"),
        api_key=api_key,
        base_url=os.getenv("OPENAI_BASE_URL"),
        temperature=0,
        max_tokens=answer_chain.max_output_tokens,
        timeout=answer_chain.request_timeout_seconds,
        max_retries=0,
    )
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", candidate["system_prompt"]),
            ("human", "用户问题：{question}\\n\\n已审核资料：\\n{evidence}"),
        ]
    )
    answer_chain._chain = prompt | model
    return True


def _get_service(
    enable_cart_drafts: bool = False, candidate: dict[str, str] | None = None
) -> SkinAssistantService:
    """按评测模式复用隔离服务，始终禁用远程模型与业务数据库。"""
    candidate_id = candidate["candidate_id"] if candidate else None
    service = _services.get((enable_cart_drafts, candidate_id))
    if service is None:
        # Provider 不读取 DATABASE_URL，轨迹/审计只保留在当前进程的内存 SQLite 中。
        engine = create_engine("sqlite+pysqlite:///:memory:")
        metadata.create_all(engine)
        service = SkinAssistantService(
            # confirmation_preview 只生成一次性确认提议；不会调用确认接口或创建草稿。
            enable_cart_drafts=enable_cart_drafts,
            embedder=LexicalOnlyEmbedder(),
            repository=SqlAssistantRepository(engine=engine),
            # 只有候选评测才允许一次模型调用；默认黄金基线仍保持零调用。
            max_model_calls_per_run=1 if candidate else 0,
        )
        if candidate:
            _configure_candidate_chain(service, candidate)
        else:
            # 不论本机 .env 如何配置，黄金评测均不允许触发回答模型。
            service.answer_chain._chain = None
        _services[(enable_cart_drafts, candidate_id)] = service
    return service


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, str]:
    """执行一条黄金题，并仅返回断言所需的可审计公开字段。"""
    del options
    variables = context.get("vars", {})
    question = str(variables.get("input", prompt))
    case_id = str(variables.get("case_id", "unknown"))
    evaluation_mode = str(variables.get("evaluation_mode", "read_only"))
    if evaluation_mode not in {"read_only", "confirmation_preview"}:
        raise ValueError(f"不支持的 evaluation_mode：{evaluation_mode}")
    candidate = _live_candidate()
    service = _get_service(
        enable_cart_drafts=evaluation_mode == "confirmation_preview", candidate=candidate
    )
    result = service.build_result(question, f"promptfoo_{case_id}")

    payload = {
        "case_id": case_id,
        "answer": result.answer,
        "query_plan": result.query_plan.model_dump(mode="json"),
        "evidence_ids": [item.id for item in result.evidence],
        "bundle_ids": [item.bundle_id for item in result.bundles],
        "actions": result.actions,
        "requires_confirmation": result.requires_confirmation,
        "model_usage": result.model_usage.model_dump(mode="json"),
        "execution": {
            "cart_drafts_enabled": service.enable_cart_drafts,
            "transaction_writes_executed": 0,
        },
        "candidate": {
            "candidate_id": candidate["candidate_id"] if candidate else None,
            "system_prompt_sha256": candidate["system_prompt_sha256"] if candidate else None,
            "applied": bool(candidate and service.answer_chain._chain is not None),
        },
    }
    return {"output": json.dumps(payload, ensure_ascii=False, sort_keys=True)}
