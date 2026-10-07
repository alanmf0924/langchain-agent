from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

AgentEventType = Literal[
    "run_started",
    "retrieval_plan",
    "message_delta",
    "tool_started",
    "tool_finished",
    "evidence_ready",
    "approval_required",
    "handoff_required",
    "run_completed",
    "run_failed",
]

Intent = Literal["recommendation", "product_knowledge", "safety", "clarify", "out_of_scope"]


class Product(BaseModel):
    """商品和库存的业务快照；推荐和下单前都应以最新快照为准。"""

    sku_id: str
    name: str
    spec: str
    # 所有跨服务金额使用整数分，避免浮点误差或整元截断真实商城价格。
    price_fen: int = Field(ge=0)
    stock: int
    on_sale: bool
    approved: bool
    tags: list[str]
    ingredients: list[str]
    # 非空成分数组也可能只是节选；只有商城明确确认完整披露时才可用于“避开成分”判断。
    ingredient_disclosure_complete: bool = False
    usage: str
    cautions: str
    # 图片仅来自商品后台的受控上传路径；空数组表示该真实商品尚未维护图片。
    image_urls: list[str] = Field(default_factory=list)


class DocumentChunk(BaseModel):
    """可被模型引用的审核资料片段，审核状态和上架状态是检索过滤条件。"""

    id: str
    sku_id: str
    title: str
    version: str
    content: str
    tags: list[str]
    approved: bool
    on_sale: bool


class Evidence(BaseModel):
    """对前端公开的最小证据集，不暴露原始知识库内部字段。"""

    id: str
    sku_id: str
    title: str
    version: str
    quote: str
    retrieval_score: float | None = None
    # 纯文本知识库没有页码；此字段保存切片定位，接入 PDF/Word 解析后可映射到页码和段落。
    source_chunk_id: str | None = None


class BundleItem(BaseModel):
    """套餐中的一个不可由模型任意增删的商品项。"""

    sku_id: str
    name: str
    spec: str
    price_fen: int = Field(ge=0)
    quantity: int = 1
    # 前端只使用服务端随本次价格/库存快照返回的主图，绝不根据 SKU 拼接静态资源。
    image_url: str = ""


class Bundle(BaseModel):
    """服务端组装的推荐套餐；价格和库存需要在确认时二次校验。"""

    bundle_id: str
    name: str
    bundle_price_fen: int = Field(ge=0)
    list_price_fen: int = Field(ge=0)
    items: list[BundleItem]
    routine: list[str]
    caution: str


class AgentRunRequest(BaseModel):
    """推荐图的外部输入；上下文只由服务端按会话归属读取。"""

    question: str = Field(min_length=2, max_length=500)
    # 前端只能引用已归属给当前 customer 的会话 ID，不能提交“历史文本”伪造记忆。
    conversation_id: str | None = Field(default=None, min_length=36, max_length=36)


class KnowledgeDocumentUpload(BaseModel):
    """上传至本地知识库的业务资料；审核字段决定它能否进入检索。"""

    document_id: str = Field(min_length=3, max_length=100)
    title: str = Field(min_length=2, max_length=200)
    content: str = Field(min_length=1, max_length=20_000)
    version: str = Field(min_length=1, max_length=50)
    sku_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    approved: bool = False
    on_sale: bool = True
    data_owner: str = Field(min_length=2, max_length=100)


class KnowledgeDocumentReceipt(BaseModel):
    document_id: str
    version: str
    chunk_count: int
    searchable: bool
    message: str


class KnowledgeSearchRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    sku_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=3, ge=1, le=10)


class KnowledgeBaseStatus(BaseModel):
    provider: str
    ready: bool
    reason: str
    document_count: int
    chunk_count: int
    vectorized_chunk_count: int


class QueryPlan(BaseModel):
    """公开、可审计的检索计划，不包含模型内部推理过程。"""

    intent: Intent
    sub_questions: list[str]
    sources: list[str]
    next_action: Literal["answer", "clarify", "stop"]


class ModelUsage(BaseModel):
    """一次回答生成的成本审计数据；缺失用量绝不以估算值伪造。"""

    model: str | None = None
    provider_attempted: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    usage_reported: bool = False
    elapsed_ms: int | None = None
    output_token_limit: int | None = None
    # 记录本次运行实际尝试次数与总预算，避免把“图可能循环”误认为无限模型调用。
    model_call_count: int = Field(default=0, ge=0)
    model_call_budget: int = Field(default=1, ge=0)
    evidence_count: int = 0
    evidence_chars: int = 0
    skipped_or_fallback_reason: str | None = None


class ModelUsageLog(ModelUsage):
    """与一次运行关联的模型成本审计记录。"""

    at: datetime
    run_id: str


class AgentEvent(BaseModel):
    """前端可展示的执行事件；payload 不应包含模型思维链或敏感原始数据。"""

    type: AgentEventType
    run_id: str
    at: datetime
    payload: dict[str, Any]


class AgentResult(BaseModel):
    """一次推荐运行的最终结构化结果，确认动作只是提议，不代表已执行。"""

    answer: str
    query_plan: QueryPlan
    model_usage: ModelUsage
    evidence: list[Evidence]
    bundles: list[Bundle]
    actions: list[dict[str, Any]]
    requires_confirmation: bool
    # 自动问答在多次澄清后仍无法识别目的时，停止继续猜测并交由人工队列处理。
    requires_handoff: bool = False


class CartDraftRequest(BaseModel):
    """写操作只信任服务端生成的一次性确认令牌和显式 confirmed 值。"""

    confirmation_token: str = Field(min_length=8)
    confirmed: bool


class CartDraftResponse(BaseModel):
    """购物车草稿创建成功后的回执，不表示订单已提交或支付完成。"""

    cart_draft_id: str
    bundle: Bundle
    status: Literal["created"]
    price_checked_at: datetime


class AuditLog(BaseModel):
    """策略判定和实际结果的审计条目，供追溯为何允许、暂停或拒绝。"""

    at: datetime
    run_id: str
    action: str
    decision: Literal["allow", "require_confirmation", "deny"]
    reason: str
    input_summary: str
    outcome: str


TraceStage = Literal["intent", "tool", "evidence", "generation", "safety", "transaction", "completed"]
TraceStatus = Literal["completed", "blocked", "skipped", "failed"]


class TraceEvent(BaseModel):
    """面向复核的公开执行事件，不含模型思维链、原始 Prompt 或敏感用户输入。"""

    at: datetime
    stage: TraceStage
    status: TraceStatus
    summary: str
    details: dict[str, Any]


class AgentTrace(BaseModel):
    """一次问答的可回查轨迹；用户原文仅以哈希和长度标识。"""

    run_id: str
    question_sha256: str
    question_length: int
    started_at: datetime
    finished_at: datetime | None = None
    events: list[TraceEvent] = Field(default_factory=list)


class EvaluationSnapshotDocument(BaseModel):
    """一次评测所冻结的知识资料元数据；内容以哈希标识，避免报告重复存储全文。"""

    document_id: str
    title: str
    version: str
    sku_ids: list[str]
    approved: bool
    on_sale: bool
    content_sha256: str


class EvaluationSnapshot(BaseModel):
    """可复跑评测的知识库与 Prompt 版本快照。"""

    snapshot_id: str
    created_at: datetime
    knowledge_sha256: str
    prompt_sha256: str
    prompt_version: str
    documents: list[EvaluationSnapshotDocument]


class EvaluationCase(BaseModel):
    """自动或人工维护的单条评测题；预期结论必须关联已冻结的资料。"""

    case_id: str = Field(min_length=3, max_length=100)
    dimension: str = Field(min_length=2, max_length=100)
    prompt: str = Field(min_length=2, max_length=500)
    expected_intent: Intent
    expected_action: Literal["answer", "clarify", "stop"]
    must_have_evidence: bool
    source_evidence_ids: list[str] = Field(default_factory=list)


class EvaluationDataset(BaseModel):
    """绑定知识快照和 Prompt 快照的可版本化题集。"""

    dataset_id: str
    created_at: datetime
    snapshot_id: str
    prompt_version: str
    source: Literal["llm_generated", "manual"]
    generator_model: str | None = None
    cases: list[EvaluationCase]


class EvaluationDatasetGenerateRequest(BaseModel):
    """要求 LLM 从当前已审核知识快照生成候选题。"""

    count: int = Field(default=8, ge=1, le=30)
    snapshot_id: str | None = Field(default=None, min_length=8, max_length=100)


class EvaluationRunRequest(BaseModel):
    """执行一个已冻结题集；默认本地确定性模式避免回归检查依赖外部网络。"""

    dataset_id: str = Field(min_length=8, max_length=100)
    baseline_run_id: str | None = Field(default=None, min_length=8, max_length=100)
    execution_mode: Literal["deterministic_local", "configured_model"] = "deterministic_local"
    enable_llm_judge: bool = False


class EvaluationHardGate(BaseModel):
    intent_matches_expected: bool
    action_matches_expected: bool
    evidence_matches_expected: bool
    safety_boundary_passed: bool
    passed: bool


class EvaluationJudgeReview(BaseModel):
    """LLM 裁判的可见结论；它只能补充质量意见，不能覆盖安全硬门。"""

    score: int = Field(ge=0, le=100)
    grounded: bool
    complete: bool
    actionable: bool
    feedback: str = Field(max_length=1000)
    suggestions: list[str] = Field(default_factory=list, max_length=5)


class EvaluationCaseResult(BaseModel):
    case_id: str
    question: str
    query_plan: QueryPlan
    answer: str
    evidence: list[Evidence]
    hard_gate: EvaluationHardGate
    judge_review: EvaluationJudgeReview | None = None
    tuning_suggestions: list[str] = Field(default_factory=list)


class EvaluationRegression(BaseModel):
    baseline_run_id: str
    hard_gate_pass_rate_delta: float
    judge_score_delta: float | None = None


class EvaluationRunReport(BaseModel):
    """一次回归运行的完整结果，可作为下次改动的基线。"""

    run_id: str
    created_at: datetime
    dataset_id: str
    snapshot_id: str
    prompt_version: str
    execution_mode: Literal["deterministic_local", "configured_model"]
    llm_judge_enabled: bool
    total_cases: int
    hard_gate_passed_cases: int
    hard_gate_pass_rate: float
    average_judge_score: float | None = None
    regression: EvaluationRegression | None = None
    tuning_suggestions: list[str] = Field(default_factory=list)
    cases: list[EvaluationCaseResult]
