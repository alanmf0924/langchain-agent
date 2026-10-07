# Promptfoo 独立黄金集评测

本目录是 **独立评测项目**，不把 Promptfoo 依赖、缓存或结果混入业务后端。它读取上级目录唯一的数据源 [`eval/golden_questions.json`](../eval/golden_questions.json)，不会复制或另造黄金题。

## Promptfoo 是什么

Promptfoo 是用于自动化评估 LLM / RAG / Agent 的开源工具。它以 Provider 调用被测系统、以测试集提供输入、以 Assertions 进行可重复判定，并产出每题通过/失败明细。本项目使用它的 Python Provider 和 Python Assertion：前者直接调用现有 LangGraph 服务，后者判定业务硬门禁。

## 本评测测什么

- 51 条黄金题：路由 intent、next_action 与可验证的业务状态。
- 证据：需要时必须命中指定证据；不得命中 `must_not_retrieve_ids`。
- 商品状态：套餐类型、预算单品降级、无匹配、资料不全、零库存、下架资料隔离。
- 交易边界：4 条 `confirmation_preview` 只验证确认提议和一次性令牌的生成，绝不提交确认或创建购物车草稿。
- 安全：`clarify` / `safety` 路径不得带套餐、交易动作或确认请求。
- 副作用：强制词法检索、禁用回答模型；除确认预览外关闭购物车草稿。预览也只写入进程内确认令牌，模型调用数和交易写入数必须为 0，绝不连接业务数据库。

这是一条**确定性离线基线**，并不等同于带真实模型、真实商城或线上流量的测评。后续若要评估某个新 Prompt，应在本目录新增单独的配置/数据集并保留本基线，不与 `golden` 结果混写。

## AutoPE 闭环

本项目现在有一条独立的 AutoPE 控制面：DSPy 生成候选，Promptfoo 判定候选，Langfuse v2 Observations API 发现线上异常/低分样本并回流为待审核考题。`autope.py` 不会写入业务默认 `SYSTEM_PROMPT` 或 `eval/golden_questions.json`；固定黄金集仍是发布前基线。

| 环节 | 产物 | 门禁 |
| --- | --- | --- |
| DSPy | `autope-candidates-v1` manifest | 候选不得删减证据、交易、纯文本等不可放宽条款 |
| Promptfoo | `autope-candidate-results.json` | 每题硬门禁通过，且候选确实加载到隔离回答链 |
| Langfuse | 不含原文的审核队列 | 人工脱敏、补齐业务预期契约、显式批准后才进入扩展题集 |

### DSPy 候选

在后端根目录将 DSPy 安装到可选 `autope` 依赖组。模型名和密钥只通过临时环境变量提供，绝不写入 brief、manifest 或报告。

```bash
cd ..
uv sync --group autope
cd promptfoo-evaluation
export AUTOPE_DSPY_MODEL='openai/gpt-4.1-mini'
export AUTOPE_DSPY_API_KEY='...'
npm run autope -- dspy-generate --output autope/local/candidates.json
```

候选 brief 位于 `autope/brief.json`。每个候选带系统提示词、Prompt/brief 哈希、训练题 ID 和生成器标识；重复候选、超出数量或删减硬条款会被拒绝。

### Promptfoo 候选门禁

候选评测仍使用 mock 商品目录、内存 SQLite、已审核资料和零交易写入。它只在隔离 Provider 中挂载候选 Prompt，并在单次调用、8 秒超时、零 SDK 重试的边界内测试，不会改动线上默认回答链。

```bash
export AUTOPE_CANDIDATE_FILE="$PWD/autope/local/candidates.json"
export AUTOPE_CANDIDATE_ID='dspy_...'
export OPENAI_API_KEY='...'
npm run eval:autope

npm run autope -- select-candidate \
  --baseline reports/golden-results.json \
  --candidate-result reports/autope-candidate-results.json \
  --output autope/local/promotion-report.json \
  --max-cost 2
```

通过只会得到 `eligible_for_human_release_review`，不是自动发布。晋级报告要求基线全绿、候选完整覆盖基线并逐题通过、所有记录只有一个已实际加载的候选 ID，且成本不超过上限。

用于 CI 的候选 manifest 必须作为受审查的源码工件放在 `autope/candidates/review/`；本地 DSPy 生成的 `generated-*.json` 仍不提交。根目录的 `autope-candidate.yml` 仅能手动触发，并要求受保护环境中的 `AUTOPE_OPENAI_API_KEY`。它只产生晋级报告，不能直接改动线上 Prompt。

### Langfuse 线上回流

读取端使用 Langfuse v2 Observations API 的 `GENERATION` 行，并强制不超过 7 天的时间窗。原始导出应放在被忽略的 `autope/local/`，不要提交。线上服务端的可选观测依赖通过 `uv sync --group observability` 安装，默认 `LANGFUSE_ENABLED=false`；启用后只发送问题哈希、长度与结构化结果摘要，不使用已弃用的 traces 读取接口。

```bash
export LANGFUSE_BASE_URL='https://cloud.langfuse.com'
export LANGFUSE_PUBLIC_KEY='pk-lf-...'
export LANGFUSE_SECRET_KEY='sk-lf-...'
npm run autope -- langfuse-fetch \
  --from '2026-10-01T00:00:00Z' \
  --to '2026-10-02T00:00:00Z' \
  --output autope/local/langfuse-export.json
npm run autope -- build-review-queue \
  --input autope/local/langfuse-export.json \
  --output autope/local/review-queue.json
```

队列只保存 Langfuse trace/observation ID、时间、异常或低分原因、输入输出哈希和长度，不复制线上问答原文。审核人应在 Langfuse 中定位原记录、人工脱敏并补齐 `expected_intent`、`expected_action`、证据允许/禁止集合；批准记录还必须保留 `source.langfuse_trace_id` 与 `source.langfuse_observation_id`。只有 `review_status: "approved"` 的 `online-...` 题会被 `promptfooconfig.autope.yaml` 合并进候选评测。明显手机号和邮箱会被拦截，但人工审核仍须确认没有其他个人信息或敏感健康描述。

```bash
npm run autope -- validate-reviewed
npm run test:autope
```

本地测试不调用 DSPy、Langfuse 或模型，覆盖硬条款拒绝、回流队列不复制原文、PII 拒绝和“候选未实际加载”不得晋级。

## 团队运行与 CI

后端根目录的 `.nvmrc` 固定 Node 24.19.0；先在根目录执行 `nvm use`，再运行评测。`.github/workflows/evaluation.yml` 会在 PR 上执行 Python 静态检查、观测适配层测试、AutoPE 控制面测试和零模型 Promptfoo 黄金门禁，并上传报告作为 30 天构建产物。模型候选评测在单独的手动工作流运行，凭据仅来自受保护环境 Secret。

## 运行

```bash
cd promptfoo-evaluation
nvm use
npm install
npm run runtime:check
npm run eval
npm run view
```

Promptfoo 0.123.1 要求 Node.js `>=22.22.0`；仓库根的 `.nvmrc` 固定 Node 24.19.0。运行脚本会把 Promptfoo 的缓存、临时数据库限制在本项目 `.promptfoo/`，并关闭遥测。

运行结果写入 `reports/golden-results.json`；Promptfoo 终端表格也会保留每条题目的通过状态和失败原因。脚本显式设置 `PROMPTFOO_PYTHON=../.venv/bin/python`，保证 Provider 与后端使用同一套依赖。

## 结构

- `tests.py`：从业务黄金集生成 Promptfoo 测例。
- `provider.py`：受控调用业务服务，返回可审计 JSON。
- `assertions.py`：硬门禁断言，含套餐、可见失败原因与确认提议状态。
- `promptfooconfig.yaml`：Promptfoo 入口。
- `autope.py`：候选 manifest、Langfuse v2 拉取/审核队列和人工晋级报告 CLI。
- `promptfooconfig.autope.yaml`：显式模型候选评测入口；不替代固定基线。
- `autope/brief.json`：DSPy 候选约束；`autope/reviewed_cases.json`：人工批准的线上回流题。
