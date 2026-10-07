# 澄肌护肤选购助手 · LangChain 后端

这是承载护肤助手及后续 OA 等后台服务的唯一后端项目。它保持原有前端 SSE 协议和购物车确认接口，因此前端不必为了后端重建而改动。

## 分层

```text
FastAPI / SSE
      └─ LangGraph 状态图（公开事件、确认令牌、审计）
      ├─ LangChain Tools（仅搜索商品、查实时库存、取审核资料）
      ├─ LangChain LCEL 回答链（Prompt | ChatOpenAI | Parser）
      ├─ Catalog（Mock 商品、库存、资料；可替换为真实适配器）
      └─ Policy（allow / require_confirmation / deny）
```

模型永远不能获得 `create_cart_draft`、改价、改库存、发积分等交易工具。创建草稿需服务端策略确认，并在执行前重新读取库存和上架状态。

## 可见工作流

推荐与确认后的创建草稿被编译成两个 LangGraph。推荐图在安全判断处发生分支；用户确认属于一次新的 HTTP 请求，因此会进入第二张执行图，而不是让模型绕过确认点。

```text
GET /api/workflows/mermaid
```

该接口仅供具备 OA `assistant:operations:read` 权限的人员使用，返回编译后图的 Mermaid 定义。

## LangGraph Studio 后端调试

除原有 FastAPI 外，项目还提供 LangGraph Server 调试入口。它注册的图 ID 为 `skin_assistant`，可在 Studio 中查看节点、分支和每个节点的状态更新。

```bash
uv sync --group dev
uv run langgraph dev --host 127.0.0.1 --port 2024
```

调试服务只需要给 `skin_assistant` 提交：

```json
{"question":"换季干燥紧绷，预算 600 元"}
```

它会在 `initialize_run` 自动补充 `run_id`。创建购物车草稿仍必须回到 FastAPI 的 `POST /api/cart-drafts` 并提交确认令牌；Studio 调试图没有交易写入入口，避免调试时绕过确认策略。

## 运行

```bash
cd skin-assistant-langchain-backend
cp .env.example .env  # 填写 PostgreSQL DATABASE_URL；模型配置可选
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

### Docker 联调

`compose.postgres.yaml` 现在同时定义 PostgreSQL 和 `app` 服务，但仍坚持由显式发布步骤执行迁移与管理员初始化，应用启动本身不会修改 Schema。Docker 内的后端不能通过 `127.0.0.1` 访问数据库；请在 `.env` 保留本机使用的 `DATABASE_URL`，并额外设置指向 Compose 服务名的 `DATABASE_URL_DOCKER`（示例已给出）。两个 URL 的用户名、口令和数据库名必须一致；若口令含 `@`、`:` 等 URL 保留字符，必须先进行 URL 编码。为避免占用开发机的默认 PostgreSQL 端口，Docker 数据库默认发布到宿主机 `5433`；容器内服务间仍使用 `postgres:5432`。

```bash
# 1. 仅启动数据库并等待健康检查通过
docker compose -f compose.postgres.yaml up -d postgres
docker compose -f compose.postgres.yaml ps

# 2. 构建后端镜像，并在同一镜像中显式发布 Schema
docker compose -f compose.postgres.yaml build app
docker compose -f compose.postgres.yaml run --rm app \
  uv run --no-sync alembic upgrade head

# 3. 只执行一次；口令以临时环境变量传入，不写入 .env
OA_BOOTSTRAP_USERNAME=admin OA_BOOTSTRAP_PASSWORD='<一次性管理员口令>' \
  docker compose -f compose.postgres.yaml run --rm \
  -e OA_BOOTSTRAP_USERNAME -e OA_BOOTSTRAP_PASSWORD app \
  uv run --no-sync python -m app.oa.bootstrap

# 3. 前台用户与 OA 员工账户分离；仅从部署 Secret 创建第一位前台用户
CUSTOMER_BOOTSTRAP_USERNAME=customer CUSTOMER_BOOTSTRAP_PASSWORD='<一次性用户口令>' \
  docker compose -f compose.postgres.yaml run --rm \
  -e CUSTOMER_BOOTSTRAP_USERNAME -e CUSTOMER_BOOTSTRAP_PASSWORD app \
  uv run --no-sync python -m app.customer.bootstrap

# 4. 再启动 HTTP 服务；浏览器访问 http://127.0.0.1:8000/docs
docker compose -f compose.postgres.yaml up -d --build app
```

`bootstrap` 对同名账号是幂等的，但不应用作日常账号管理工具。需要停止联调环境时运行 `docker compose -f compose.postgres.yaml down`；这不会删除 PostgreSQL 数据卷。仅在确认要清空本地数据库时才额外使用 `--volumes`。

未配置 `OPENAI_API_KEY` 时，服务仍使用确定性、可测试的受证据约束回答；可完整验证工具、SSE、确认与审计流程。配置时使用任意 OpenAI Chat Completions 兼容地址，不要把密钥提交到仓库。

`DATABASE_URL` 是全项目唯一的 PostgreSQL 数据源。Alembic 管理所有服务的 Schema：助手运行轨迹、审计、确认令牌和草稿回执，以及商品后台的商品与图片元数据；重启后仍可按 `run_id` 复核，并且令牌只可成功消费一次。单元测试才会使用隔离的 SQLite 内存库，应用运行时不会回退到 SQLite。真实购物车仍必须接入商城权威服务。

## 数据库与迁移

生产或预发环境应由发布流程在启动应用前执行迁移，应用本身不在启动时改动 Schema：

```bash
# 查看尚未执行的迁移
uv run alembic current

# 应用迁移
uv run alembic upgrade head

# 新增同一项目内任意后端模块的 Schema 后生成候选迁移，再人工审阅 SQL
uv run alembic revision --autogenerate -m "add oa rbac"
uv run alembic upgrade head --sql
```

`DATABASE_URL` 必须使用 SQLAlchemy 的 psycopg 驱动，例如
`postgresql+psycopg://skin_assistant:change_me@127.0.0.1:5432/skin_assistant`。不要把真实数据库口令写入 `.env.example`、代码或仓库；生产改用部署平台的 Secret Manager。后续 OA/RBAC、用户、菜单和审批等表都在本项目的 `app/` 模块和同一个 Alembic 迁移链中增加，不再创建第二个后端服务或第二套数据库。

## OA / RBAC 模块

OA 后台接口已作为同一 FastAPI 应用中的 `app/oa/` 模块运行，使用相同的 PostgreSQL 和 Alembic 链，而不是另起 `oa-server`。当前提供：

- `POST /api/v1/auth/login`：账号密码登录，返回短期 Bearer access token，并签发 HttpOnly refresh cookie 与 CSRF cookie；
- `POST /api/v1/auth/refresh`、`POST /api/v1/auth/logout`、`GET /api/v1/auth/me`：轮换会话、退出与读取用户/角色/权限；
- `GET /api/v1/routes`：由服务端按当前权限计算 OA 菜单，前端只负责渲染，不能自行扩大权限；
- `/api/v1/system/departments`、`/api/v1/system/permissions`、`/api/v1/system/roles`：部门、权限点、角色的查询、创建、更新与安全删除接口；
- `/api/v1/system/users`：OA 员工账号的查询、创建、更新、停用与口令重置接口。账号不提供物理删除，以保留会话与审计可追溯性。

OA 身份数据全部使用 `oa_users`、`oa_roles`、`oa_permissions`、`oa_departments` 等 `oa_*` 表；它们与商城前台消费者账号 `customers` 完全隔离。前台账户不复用 OA 角色：消费者通过当前 `customer_id` 校验其个人资源归属；OA 才使用 RBAC。每个管理接口都由服务端权限点保护：`system:{department|permission|role|user}:{read|write}`。助手运维接口额外使用 `assistant:{knowledge|operations|evaluation}:{read|write}`；首次 bootstrap 的 `super_admin` 持有 `*:*:*`，可先用于 OA 前端联调。

首次部署顺序如下（不要在应用启动时自动迁移）：

```bash
uv run alembic upgrade head
OA_BOOTSTRAP_USERNAME=admin OA_BOOTSTRAP_PASSWORD='change-this-once' \
uv run python -m app.oa.bootstrap
CUSTOMER_BOOTSTRAP_USERNAME=customer CUSTOMER_BOOTSTRAP_PASSWORD='change-this-once' \
uv run python -m app.customer.bootstrap
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 前台账户与邮箱找回

当前前台账户支持 `POST /api/customer/auth/register`、账号密码登录、刷新/退出、登录态改密码，以及忘记密码请求。注册邮箱写入 `customers.email`；忘记密码请求只将邮箱 SHA-256 摘要写入待投递记录，接口不会假称邮件已经送达，也不会透露邮箱是否已注册。注册阶段不要求邮箱验证，用户可直接登录。

后续完成 SMTP/邮件供应商接入并验收后，将 `CUSTOMER_EMAIL_DELIVERY_ENABLED=true`：届时启用验证邮件和一次性重置链接。验证和重置 token 仅存 SHA-256 哈希，验证链接 24 小时有效、重置链接 30 分钟有效且只能使用一次；改密或重置会撤销全部旧会话。

前台用户不获取 OA 权限列表。咨询和购物车草稿由服务端要求有效的 customer access token；浏览器使用 HttpOnly refresh Cookie 维持 7 天登录，页面重开后由它换取新的短期 access token。前端仅在本机保存可展示的账户摘要，不保存 access token；后续地址、订单、收藏和持久会话必须以当前 `customer_id` 做所有权筛选，不能只靠前端隐藏入口。

邮件由 `CUSTOMER_EMAIL_DELIVERY_ENABLED` 和 `CUSTOMER_MAIL_MODE` 共同控制：默认只记录，绝不调用邮件适配器；开启后，本地可用 `file` 模式把 `.eml` 写入仅开发机可见的 `CUSTOMER_MAIL_OUTBOX_DIR` 来验证完整链接流程，`log` 模式只验证触发动作且不输出正文/token；预发/生产必须配置 `smtp` 及其 Secret。生产还强制 `CUSTOMER_COOKIE_SECURE=true` 和 HTTPS 的 `CUSTOMER_APP_BASE_URL`。管理员初始化口令与 SMTP 密钥只应由短期部署 Secret 注入，不能写入仓库或长驻环境变量。

## 模型调用与超时护栏

当前推荐图没有模型循环，只有一个回答节点；仍通过 `MODEL_MAX_CALLS_PER_RUN=1` 明确限制单次运行的模型调用预算。`MODEL_REQUEST_TIMEOUT_SECONDS=8` 和 SDK `max_retries=0` 防止服务商超时后继续重试消耗 Token；`LANGGRAPH_RECURSION_LIMIT=24` 防止未来图改动引入无限节点循环。模型超时、未配置或预算耗尽时，服务保留已审核证据和套餐判断，返回确定性说明，并在 `model_usage` 与审计轨迹中标识原因。

## 商城读模型开关

默认 `CATALOG_SOURCE=mock`，明确标识为本地演练数据。商品后台完成迁移后，可设置 `CATALOG_SOURCE=database`：公开目录、候选检索和确认前库存/价格复核都会读取同一数据库中的商品；商品必须同时为 `assistant_approved=true` 与 `on_sale=true` 才能进入 Agent，且复核时库存必须大于零。数据库故障不会回退到 Mock。设置 `CATALOG_SOURCE=mall` 后，后端会使用 OAuth2 Client Credentials 调用候选商品与实时可售性接口；缺少任一商城配置会使服务启动失败，不会静默回退到 Mock。金额在 API 内统一使用整数分字段（例如 `price_fen`、`bundle_price_fen`），前端仅负责格式化人民币。详细字段、刷新 SLA 与测试 SKU 见 `docs/MALL_READ_MODEL_CONTRACT_DRAFT.md`。

## 商品后台

商品后台接口位于 `/api/v1/catalog`，均要求 OA `catalog:product:read` 或 `catalog:product:write` 权限（`super_admin` 可直接联调）。先以 `multipart/form-data` 调用 `POST /api/v1/catalog/uploads/images` 上传 JPEG、PNG 或 WebP（单张最多 5 MiB）；响应的 `id` 放入 `POST /api/v1/catalog/products` 的 `image_ids`。商品创建和更新支持 SKU、标题、规格、整数分价格、库存、说明、用法、注意事项、完整成分表、场景标签、上架与 Agent 审核状态。更新必须携带上次读取的 `expected_revision`，发生并发覆盖时返回 `409`。

面向 OA 前端联调的完整请求示例、字段约束和状态码见 [`docs/CATALOG_OA_API.md`](docs/CATALOG_OA_API.md)。

图片只按服务端生成的对象键写入 `PRODUCT_UPLOAD_DIR`（默认 `data/runtime/uploads/products`），不会信任原文件名；会核验 MIME 声明、文件头、类型一致性和大小。生产部署需要为该目录挂载持久卷，或替换为对象存储适配器。每次上传、创建和更新都写入 OA 审计日志。

## 验证

```bash
uv run pytest
curl -N -X POST http://127.0.0.1:8000/api/agent/runs \
  -H 'content-type: application/json' \
  -d '{"question":"换季干燥紧绷，预算 600 元"}'
```

应依次收到 `run_started`、工具事件、`evidence_ready`、`approval_required` 与 `run_completed`。`approval_required` 中的 `confirmation_token` 传给 `POST /api/cart-drafts`，仅 `confirmed: true` 才能创建草稿。

## V1 黄金问题业务评审报告

以下命令会逐条运行 `eval/golden_questions.json` 中全部黄金问题，复用实际
`build_result()` 问答工作流，并导出包含 QueryPlan、回答、证据 ID/版本、每题工具审计、
安全动作和交易动作的报告。

```bash
# Markdown（默认路径：eval/reports/v1_golden_report.md）
uv run python -m eval.v1_report

# JSON（适合交给 BI 或后续自动校验）
uv run python -m eval.v1_report \
  --format json \
  --output eval/reports/v1_golden_report.json

# 只验证报告生成器
uv run pytest tests/test_v1_report.py
```

## 前台黄金集端到端测评

前台测评复用同一份 `eval/golden_questions.json`，但会真的启动隔离的 FastAPI 与 Vite，逐题
完成登录态恢复、页面输入、SSE 消费、公开结果渲染及刷新后的会话恢复。普通问答与“仅确认预览”
分开运行，后者只断言确认卡可展示；两种模式都不会请求 `POST /api/cart-drafts`。

```bash
# 需要 Node 20+ 与已安装的前端依赖
npm run test:frontend-eval
```

该命令强制使用本地词法检索、Mock 商品源、零模型调用预算和临时 SQLite，不会连接当前 Docker
数据库、调用外部模型或创建购物车草稿。逐题报告输出到
`eval/reports/frontend_e2e_report.json`；失败时会在 `eval/reports/frontend_e2e_failures/` 留下截图。

报告运行强制禁用外部模型调用和购物车草稿，无论本机 `.env` 是否配置模型或开启了
`ENABLE_CART_DRAFTS`。安全问题仍走 `stop_recommendation`，只读查询仍记录实际工具审计；
报告不会创建任何交易草稿或执行其他写操作。

## 检索基线与 Reranker 准入

黄金题现在为每条问题显式维护 `expected_evidence_ids`；需证据题还维护
`must_not_retrieve_ids`，以便同时评估正确召回与审核/在售违规召回。下面的命令会在不调用
外部模型、不产生交易写入的前提下，导出 Recall@5、MRR、Top 3 表现、引用正确率、违规召回率，
并给出是否应接入 Reranker 的门禁结论：

```bash
# Markdown（默认：eval/reports/retrieval_baseline_report.md）
UV_CACHE_DIR=/tmp/skin-assistant-uv-cache uv run python -m eval.retrieval_report

# JSON
UV_CACHE_DIR=/tmp/skin-assistant-uv-cache uv run python -m eval.retrieval_report \
  --format json \
  --output eval/reports/retrieval_baseline_report.json
```

Reranker 仅在审核且在售语料为 20–50 份、Recall@5 ≥ 95%、违规召回率为 0，且 Top 3
仍存在第 4–5 位可挽回的正确资料时建议接入。当前本地仿真语料不足 20 份，报告会给出
`not_now`，而不是人为补充虚假资料凑门槛。

## EvalOps：知识库与 Prompt 的持续回归

`/api/evaluations` 将一次评测绑定到知识库资料版本、资料全文哈希和回答 Prompt 哈希。默认
运行 `deterministic_local`，用真实问答图验证意图、动作、证据和安全边界，不依赖外部网络；
评测使用隔离服务，始终关闭购物车草稿。

```bash
# 1. 先冻结当前知识库和 Prompt
curl -X POST http://127.0.0.1:8000/api/evaluations/snapshots

# 2a. 无需外部模型：把既有 V1 30 条题注册为第一份基线题集
curl -X POST http://127.0.0.1:8000/api/evaluations/datasets/v1-golden

# 2b. 配置并显式开启 Eval LLM 后，基于上一步返回的 snapshot_id 生成候选题集
#     EVAL_LLM_ENABLED=true，密钥仅写入本地 .env；也可以省略 snapshot_id 让服务即时创建快照
curl -X POST http://127.0.0.1:8000/api/evaluations/datasets/generate \
  -H 'content-type: application/json' \
  -d '{"count":8,"snapshot_id":"snapshot_..."}'

# 3. 运行题集；第一次返回的 run_id 可作为下一次改动后的 baseline_run_id
curl -X POST http://127.0.0.1:8000/api/evaluations/runs \
  -H 'content-type: application/json' \
  -d '{"dataset_id":"dataset_...","execution_mode":"deterministic_local"}'
```

自动题集中的每道题都必须引用快照中的资料 ID；生成结果越界、重复或让安全题引用商品资料时会被拒绝。
开启 `enable_llm_judge` 可以取得证据一致性、完整性和可执行性的质量建议，但它不能覆盖安全、证据
和交易确认硬门。`baseline_run_id` 会输出硬门通过率与裁判分数的变化，供改 Prompt 或知识库后回归判断。

## 可复核 Tracing

每次 `POST /api/agent/runs` 完成后，可使用 SSE 事件中的 `run_id` 查询公开轨迹：

```bash
curl http://127.0.0.1:8000/api/traces/run_...
```

轨迹按阶段记录意图计划、每个只读工具的策略和结果数量、命中文档 ID/标题/版本/切片 ID、回答生成或降级原因、
安全停止和交易确认状态。因此检索漏召回时可直接查看 `retrieve_evidence` 的 `result_count` 与 `sources`，而非凭回答文案猜测。
为避免暴露敏感信息，它不记录用户原文、原始 Prompt 或模型思维链；当前 JSON 文本资料只提供文档和切片定位，接入
PDF/Word 解析器并保留页码/段落元数据后，`source_chunk_id` 可进一步映射为页码和段落。
