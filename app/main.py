from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.customer.commerce_router import router as customer_commerce_router
from app.customer.conversations import (
    ConversationHistoryResponse,
    latest_conversation_response,
    prepare_conversation_run,
    save_assistant_message,
)
from app.customer.database import get_session
from app.customer.dependencies import get_current_customer
from app.customer.models import Customer, CustomerBase
from app.customer.router import router as customer_router
from app.evaluation import EvaluationModelNotConfigured, EvaluationOps
from app.models import (
    AgentEvent,
    AgentResult,
    AgentRunRequest,
    AgentTrace,
    CartDraftRequest,
    CartDraftResponse,
    EvaluationDataset,
    EvaluationDatasetGenerateRequest,
    EvaluationRunReport,
    EvaluationRunRequest,
    EvaluationSnapshot,
    KnowledgeBaseStatus,
    KnowledgeDocumentReceipt,
    KnowledgeDocumentUpload,
    KnowledgeSearchRequest,
)
from app.oa.api_contract import request_id_for, response_body
from app.oa.catalog_router import public_router as product_public_router
from app.oa.catalog_router import router as product_router
from app.oa.commerce_router import router as commerce_router
from app.oa.config import get_oa_settings
from app.oa.dependencies import require_permission
from app.oa.models import User as OaUser
from app.oa.router import router as oa_router
from app.observability import AgentObservability
from app.service import SkinAssistantService


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """端到端评测使用独立 SQLite 时才创建前台客户表，正式运行仍只能走 Alembic。"""
    if os.getenv("FRONTEND_E2E_EVALUATION", "false").lower() == "true":
        from app.customer.database import engine as customer_engine

        async with customer_engine.begin() as connection:
            await connection.run_sync(CustomerBase.metadata.create_all)
    yield


app = FastAPI(
    title="澄肌护肤选购助手 API",
    version="0.1.0",
    description="""
面向护肤选购助手前端的接口服务。

核心问答使用 **POST SSE**：请用 `fetch + ReadableStream` 消费 `/api/agent/runs`，
不要用只支持 GET 的 `EventSource`。检索证据、价格、库存和确认令牌都以服务端结果为准。
""",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "问答", "description": "用户咨询、证据与推荐套餐的 SSE 输出。"},
        {"name": "商品", "description": "前台可展示商品目录。"},
        {"name": "商品后台", "description": "商品图片、主数据、审核与上架维护。"},
        {"name": "知识库", "description": "资料上传、审核状态与检索调试。"},
        {"name": "购物车", "description": "必须经用户确认的草稿创建动作。"},
        {"name": "评测", "description": "知识库/Prompt 快照、自动评测集与回归报告。"},
        {"name": "运维", "description": "健康检查、工作流、审计与成本排查。"},
        {"name": "前台用户鉴权", "description": "前台客户登录、续期、退出与当前会话。"},
        {
            "name": "OA 鉴权与权限",
            "description": "多用户、多角色、路由和按钮权限的服务端鉴权接口。",
        },
    ],
)
oa_settings = get_oa_settings()
# 前台与 OA 的 Refresh Cookie 都需要凭据 CORS；生产仅接受明确列出的受信任来源。
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(
        {
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:5179",
            "http://localhost:5179",
            *oa_settings.cors_origins,
        }
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_oa_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
    """为每个请求提供可在响应、日志和前端报错间对应的追踪标识。"""
    incoming = request.headers.get("x-request-id", "").strip()
    request.state.request_id = incoming[:128] if incoming else uuid.uuid4().hex
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


def is_oa_api(request: Request) -> bool:
    return request.url.path.startswith("/api/v1/")


@app.exception_handler(HTTPException)
async def oa_http_exception_handler(request: Request, error: HTTPException) -> JSONResponse:
    if not is_oa_api(request):
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})
    message = error.detail if isinstance(error.detail, str) else "请求未能完成"
    return JSONResponse(
        status_code=error.status_code,
        headers=error.headers,
        content=response_body(
            status_code=error.status_code,
            message=message,
            data=None,
            request_id=request_id_for(request),
            code=error.status_code,
            errors=None if isinstance(error.detail, str) else error.detail,
        ),
    )


@app.exception_handler(RequestValidationError)
async def oa_validation_exception_handler(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    if not is_oa_api(request):
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": error.errors()})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=response_body(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            message="请求参数校验失败",
            data=None,
            request_id=request_id_for(request),
            code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors=error.errors(),
        ),
    )
# 助手和 OA/RBAC 复用 DATABASE_URL；Schema 必须先由 Alembic 发布，不能运行时自动建表。
service = SkinAssistantService()
# 前台端到端测评显式启用本地确定性模式：不读取外部回答模型，也不暴露购物车草稿。
# 该开关只服务于隔离评测环境，正常运行时不改变线上问答与交易策略。
if os.getenv("EVALUATION_DETERMINISTIC_LOCAL", "false").lower() == "true":
    service.answer_chain._chain = None
    service.enable_cart_drafts = (
        os.getenv("FRONTEND_E2E_CONFIRMATION_PREVIEW", "false").lower() == "true"
    )
# 评测快照、题集和运行结果在 V1 中仅保留于进程内；生产环境应改为持久化存储。
evaluation_ops = EvaluationOps()
agent_observability = AgentObservability()


app.include_router(oa_router)
app.include_router(product_router)
app.include_router(commerce_router)
app.include_router(product_public_router)
app.include_router(customer_router)
app.include_router(customer_commerce_router)


@app.get(
    "/api/health",
    tags=["运维"],
    summary="服务健康检查",
    description="确认 HTTP 服务进程可访问；不代表向量模型或生成模型调用成功。",
)
def health() -> dict[str, str]:
    """仅表示 HTTP 进程存活，不能代替一次真实模型调用验证。"""
    return {"status": "ok", "mode": "langchain-tools-and-grounded-chain"}


@app.get(
    "/api/catalog/products",
    tags=["商品"],
    summary="获取可展示商品",
    description="仅返回当前已审核、已上架商品。用户确认创建草稿前，服务端仍会重新校验库存与上架状态。",
)
def list_products():
    """返回可展示的商品目录；购买前仍须走实时库存校验。"""
    return service.available_products()


@app.post(
    "/api/knowledge/documents",
    response_model=KnowledgeDocumentReceipt,
    tags=["知识库"],
    summary="上传或更新知识资料",
    description="相同 document_id 会替换旧版本的全部切块。仅 approved=true 且 on_sale=true 的资料可进入问答检索。当前仅支持 JSON 文本，不支持文件上传。",
)
def upload_knowledge_document(
    request: KnowledgeDocumentUpload,
    _: OaUser = Depends(require_permission("assistant:knowledge:write")),
) -> KnowledgeDocumentReceipt:
    """上传版本化资料；未审核或下架资料会入库但不能进入 RAG 检索。"""
    return service.knowledge_base.ingest(request)


@app.get(
    "/api/knowledge/documents",
    tags=["知识库"],
    summary="列出知识资料",
    description="返回已上传资料的版本、关联 SKU、审核状态和责任人；不返回内部切块和向量。",
)
def list_knowledge_documents(
    _: OaUser = Depends(require_permission("assistant:knowledge:read")),
) -> list[KnowledgeDocumentUpload]:
    """用于审核资料版本、责任人与上架状态，不返回切块实现细节。"""
    return service.knowledge_base.list_documents()


@app.get(
    "/api/knowledge/status",
    response_model=KnowledgeBaseStatus,
    tags=["知识库"],
    summary="获取知识库状态",
    description="用于后台展示检索提供方、就绪状态和向量化覆盖率；不会泄露密钥或原始请求。",
)
def knowledge_status(
    _: OaUser = Depends(require_permission("assistant:knowledge:read")),
) -> KnowledgeBaseStatus:
    """返回当前检索模式与向量化覆盖率，不加载模型或暴露敏感配置。"""
    return service.knowledge_base.status()


@app.post(
    "/api/knowledge/search",
    tags=["知识库"],
    summary="调试知识检索",
    description="后台调试入口。正式用户问答应调用 /api/agent/runs，由服务端先判断意图再检索。",
)
def search_knowledge(
    request: KnowledgeSearchRequest,
    _: OaUser = Depends(require_permission("assistant:knowledge:read")),
):
    """本地检索调试入口；正式问答仍通过 Agent 工作流调用同一知识库。"""
    return service.knowledge_base.search(request.question, request.sku_ids, request.limit)


@app.get(
    "/api/workflows/mermaid",
    tags=["运维"],
    summary="导出实际工作流图",
    description="返回 recommendation 与 cart_draft 两张已编译 LangGraph 的 Mermaid 定义，适合开发调试或后台可视化。",
)
def workflow_mermaid(
    _: OaUser = Depends(require_permission("assistant:operations:read")),
) -> dict[str, str]:
    """导出当前实际执行的 LangGraph Mermaid 图，而不是手写示意图。"""
    return service.workflow_mermaid()


@app.post(
    "/api/agent/runs",
    tags=["问答"],
    summary="发起选购问答（SSE）",
    description="POST 后以 text/event-stream 返回 run_started、retrieval_plan、证据、回答和最终结果。会话消息会按当前登录客户持久化，服务端最多使用受限的近期上下文；不得将模型内容作为交易授权。",
    responses={
        200: {"description": "SSE 事件流。最终 run_completed.payload.result 为完整结构化回答。"},
        401: {"description": "未登录、令牌无效或会话已失效。"},
        422: {"description": "问题为空、长度不足或超过 500 字符。"},
    },
)
async def run_agent(
    request: AgentRunRequest,
    http_request: Request,
    customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """将后端可公开的执行事件转为 SSE，供前端逐步展示而非暴露内部推理。"""
    prepared = await prepare_conversation_run(
        session, customer.id, request.conversation_id, request.question
    )

    async def event_stream() -> AsyncIterator[str]:
        observer = agent_observability.start_run(
            getattr(http_request.state, "request_id", "unknown"), prepared.agent_question
        )
        try:
            if prepared.should_handoff:
                run_id = f"handoff_{uuid.uuid4().hex}"
                handoff_result = service.handoff_result(prepared.query_plan)
                observer.record_result(run_id, handoff_result, "handoff")
                await save_assistant_message(
                    session, prepared.conversation, handoff_result.answer, "complete", handoff_result
                )
                handoff_events = [
                    AgentEvent(
                        type="run_started",
                        run_id=run_id,
                        at=service.now(),
                        payload={
                            "summary": "已接收本轮咨询",
                            "conversation_id": prepared.conversation.id,
                        },
                    ),
                    AgentEvent(
                        type="retrieval_plan",
                        run_id=run_id,
                        at=service.now(),
                        payload=prepared.query_plan.model_dump(mode="json"),
                    ),
                    AgentEvent(
                        type="message_delta",
                        run_id=run_id,
                        at=service.now(),
                        payload={"text": handoff_result.answer, "summary": "自动咨询已安全收口"},
                    ),
                    AgentEvent(
                        type="handoff_required",
                        run_id=run_id,
                        at=service.now(),
                        payload={"summary": "已提交人工导购待接入"},
                    ),
                    AgentEvent(
                        type="run_completed",
                        run_id=run_id,
                        at=service.now(),
                        payload={
                            "result": handoff_result.model_dump(mode="json"),
                            "summary": "已转人工待接入",
                        },
                    ),
                ]
                for event in handoff_events:
                    yield f"data: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                return

            saved_assistant_message = False
            async for event in service.run_events(prepared.agent_question):
                if event.type == "run_started":
                    event = event.model_copy(
                        update={
                            "payload": {
                                **event.payload,
                                "conversation_id": prepared.conversation.id,
                            }
                        }
                    )
                if event.type == "run_completed" and not saved_assistant_message:
                    result = AgentResult.model_validate(event.payload["result"])
                    observer.record_result(event.run_id, result, "completed")
                    await save_assistant_message(
                        session, prepared.conversation, result.answer, "complete", result
                    )
                    saved_assistant_message = True
                elif event.type == "run_failed" and not saved_assistant_message:
                    observer.record_failure(event.run_id, "agent_run_failed")
                    summary = event.payload.get("summary")
                    content = summary if isinstance(summary, str) else "本次咨询未能完成，请稍后重试。"
                    await save_assistant_message(session, prepared.conversation, content, "failed")
                    saved_assistant_message = True
                # SSE 每条消息都保持 JSON 结构，前端可按 type 渲染工具、证据和确认卡。
                yield f"data: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"
        finally:
            observer.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get(
    "/api/customer/conversations/latest",
    response_model=ConversationHistoryResponse,
    tags=["问答"],
    summary="恢复当前客户最近一次咨询",
    description="仅返回当前登录客户的消息与已公开结构化结果；会话上下文和人工待接入状态均由服务端校验。",
)
async def latest_customer_conversation(
    customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_session),
) -> ConversationHistoryResponse:
    """刷新页面时恢复客户自己的最近一次咨询，不暴露其他客户消息。"""
    return await latest_conversation_response(session, customer.id)


@app.post(
    "/api/cart-drafts",
    response_model=CartDraftResponse,
    tags=["购物车"],
    summary="确认后创建购物车草稿",
    description="只接受服务端在 approval_required 事件中给出的一次性确认令牌。用户必须显式传 confirmed=true；服务端会再次校验令牌、价格、库存和上架状态。",
    responses={
        401: {"description": "未登录、令牌无效或会话已失效。"},
        409: {"description": "未确认、确认令牌无效/过期，或库存、价格、上架状态发生变化。"},
        422: {"description": "确认令牌或 confirmed 字段不符合格式。"},
    },
)
def create_cart_draft(
    request: CartDraftRequest, _: Customer = Depends(get_current_customer)
) -> CartDraftResponse:
    """确认后的写操作入口；令牌、确认状态和库存均由服务端重新校验。"""
    try:
        cart_draft_id, bundle = service.create_cart_draft(
            request.confirmation_token, request.confirmed
        )
    except PermissionError as error:
        # 未确认属于业务冲突，而不是认证失败，前端可以继续展示确认卡。
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return CartDraftResponse(
        cart_draft_id=cart_draft_id, bundle=bundle, status="created", price_checked_at=service.now()
    )


@app.get(
    "/api/audit-logs",
    tags=["运维"],
    summary="查看审计记录",
    description="用于本地联调和人工复核策略判定、工具调用与结果。PostgreSQL 持久化后可跨重启回查。",
)
def list_audit_logs(_: OaUser = Depends(require_permission("assistant:operations:read"))):
    """返回助手持久化边界内的审计记录，便于本地联调和人工复核。"""
    return service.audit_logs


@app.get(
    "/api/traces/{run_id}",
    response_model=AgentTrace,
    tags=["运维"],
    summary="按运行 ID 查询可复核执行轨迹",
    description="返回意图、工具、证据、生成、安全和交易阶段的公开事实；不包含模型思维链、原始 Prompt 或用户原文。",
    responses={404: {"description": "运行不存在，或未配置持久化时服务已经重启。"}},
)
def get_agent_trace(
    run_id: str,
    _: OaUser = Depends(require_permission("assistant:operations:read")),
) -> AgentTrace:
    """支持从一次回答逆向定位路由、检索、证据或策略边界问题。"""
    trace = service.get_trace(run_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="运行轨迹不存在")
    return trace


@app.get(
    "/api/model-usage-logs",
    tags=["运维"],
    summary="查看生成模型用量记录",
    description="返回模型调用、零调用与降级原因；不包含 Prompt、用户原文或任何密钥。PostgreSQL 持久化后可跨重启回查。",
)
def list_model_usage_logs(_: OaUser = Depends(require_permission("assistant:operations:read"))):
    """返回模型调用、零调用与降级的成本审计数据；不包含 Prompt 或密钥。"""
    return service.model_usage_logs


@app.post(
    "/api/evaluations/snapshots",
    response_model=EvaluationSnapshot,
    tags=["评测"],
    summary="冻结当前知识库与 Prompt 版本",
    description="记录资料全文哈希、审核状态、版本和回答 Prompt 哈希；后续评测不会读取可变的实时资料。",
)
def create_evaluation_snapshot(
    _: OaUser = Depends(require_permission("assistant:evaluation:write")),
) -> EvaluationSnapshot:
    """为自动生成题集和回归运行创建可追溯的事实源快照。"""
    return evaluation_ops.snapshot(service)


@app.post(
    "/api/evaluations/datasets/generate",
    response_model=EvaluationDataset,
    tags=["评测"],
    summary="用 Eval LLM 从冻结资料生成候选评测集",
    description="仅在 EVAL_LLM_ENABLED=true 且配置 Eval 密钥时调用外部模型；每题必须引用当前快照中的资料 ID。",
    responses={503: {"description": "Eval LLM 未显式开启或未配置密钥。"}},
)
def generate_evaluation_dataset(
    request: EvaluationDatasetGenerateRequest,
    _: OaUser = Depends(require_permission("assistant:evaluation:write")),
) -> EvaluationDataset:
    """先冻结资料再生成题目，避免题集和被测知识库版本漂移。"""
    try:
        snapshot = (
            evaluation_ops.get_snapshot(request.snapshot_id)
            if request.snapshot_id
            else evaluation_ops.snapshot(service)
        )
        return evaluation_ops.create_generated_dataset(snapshot.snapshot_id, request.count)
    except EvaluationModelNotConfigured as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        # 生成结果若越过证据、数量或结构契约，不保存为题集，等待重试或人工处理。
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post(
    "/api/evaluations/datasets/v1-golden",
    response_model=EvaluationDataset,
    tags=["评测"],
    summary="将 V1 30 条黄金题注册为版本化基线题集",
    description="不调用外部模型。题集仍会绑定调用时的知识库和 Prompt 快照，可立刻用于首次基线与后续回归。",
)
def create_v1_golden_evaluation_dataset(
    _: OaUser = Depends(require_permission("assistant:evaluation:write")),
) -> EvaluationDataset:
    """让团队在尚未启用自动出题前，也能在同一平台形成首份可比较的基线。"""
    snapshot = evaluation_ops.snapshot(service)
    return evaluation_ops.create_manual_dataset(
        snapshot.snapshot_id, evaluation_ops.v1_golden_cases()
    )


@app.post(
    "/api/evaluations/runs",
    response_model=EvaluationRunReport,
    tags=["评测"],
    summary="运行冻结评测集并生成回归结果",
    description="默认 deterministic_local，不调用问答模型。enable_llm_judge=true 时需显式配置 Eval LLM；安全、证据和交易边界始终由确定性硬门判定。",
    responses={
        404: {"description": "评测集或基线运行不存在。"},
        503: {"description": "Eval LLM 未配置。"},
    },
)
def run_evaluation(
    request: EvaluationRunRequest,
    _: OaUser = Depends(require_permission("assistant:evaluation:write")),
) -> EvaluationRunReport:
    """执行隔离的问答服务，保证评测本身不创建购物车草稿或污染线上审计。"""
    try:
        return evaluation_ops.run(
            dataset_id=request.dataset_id,
            execution_mode=request.execution_mode,
            baseline_run_id=request.baseline_run_id,
            enable_llm_judge=request.enable_llm_judge,
        )
    except EvaluationModelNotConfigured as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get(
    "/api/evaluations/runs/{run_id}",
    response_model=EvaluationRunReport,
    tags=["评测"],
    summary="查看历史评测报告",
)
def get_evaluation_run(
    run_id: str,
    _: OaUser = Depends(require_permission("assistant:evaluation:read")),
) -> EvaluationRunReport:
    """前端可读取此报告展示当前基线、维度失败项和调优建议。"""
    report = evaluation_ops.store.runs.get(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="评测运行不存在")
    return report
