# 澄肌护肤选购助手 API（V1）

本文档以当前 FastAPI 实现为准，供 Vue 前端联调。接口根地址为 `http://127.0.0.1:8000`，交互式 OpenAPI 为 `/docs`，机器可读定义为 `/openapi.json`。

## 联调约定

- 请求和普通响应均为 `application/json; charset=utf-8`。
- 问答接口是 **POST SSE**：响应为 `text/event-stream`，前端应用 `fetch` 与 `ReadableStream` 逐块读取，不能用只支持 GET 的 `EventSource`。
- 开发环境 CORS 白名单：`http://127.0.0.1:5173`、`http://localhost:5173`、`http://127.0.0.1:5179`、`http://localhost:5179`。
- 所有金额字段均为人民币整数分（`*_fen`）；前端仅负责格式化展示，时间为 ISO 8601 UTC 字符串。
- 所有可展示事实应来自 `evidence`、`bundles` 与服务端商品接口；前端不可自行拼接价格、库存或确认令牌。

## 前台账户（当前为邮箱记录阶段）

前台注册邮箱保存在 `customers.email`，用于后续的邮箱验证和密码找回；当前 `CUSTOMER_EMAIL_DELIVERY_ENABLED=false` 时不会调用 SMTP 或声称邮件已经发送。忘记密码请求会以邮箱 SHA-256 摘要保存为 `pending_delivery` 记录，供后续邮件队列接入；无论邮箱是否存在，均返回相同状态和提示，避免枚举账户。

| 方法与路径 | 请求体 | 成功响应 | 当前阶段说明 |
| --- | --- | --- | --- |
| `POST /api/customer/auth/register` | `username`、`email`、`display_name`、`password` | `202`、`{ "message": "注册成功…" }` | 保存账户和邮箱后可直接登录。账号为 3～64 位，可使用字母、数字、`. _ @ -`，因此可以直接填邮箱；密码至少 12 位，且含大小写字母和数字。 |
| `POST /api/customer/auth/login` | `username`、`password` | `200`、`access_token`、`expires_in`、`user`；并设置 refresh/CSRF Cookie | refresh 会话与 Cookie 均为 7 天；页面重开时前端用其换取新短期 access token，不把 access token 持久写入本机。 |
| `POST /api/customer/auth/password-reset-requests` | `email` | `202`、通用 `message` | 仅记录找回请求；不投递邮件，也不返回 token。 |
| `POST /api/customer/auth/refresh` | 无；带 refresh Cookie 和 `X-CSRF-Token` | `200`、新 access token | 浏览器请求必须携带 Cookie。 |
| `POST /api/customer/auth/logout` | 无；带 refresh Cookie 和 `X-CSRF-Token` | `204` | 撤销当前 refresh 会话并清 Cookie。 |
| `GET /api/customer/auth/me` | `Authorization: Bearer <access_token>` | `200`、当前用户 | 用于恢复页面会话。 |

注册示例：

```json
{
  "username": "ming",
  "email": "ming@example.com",
  "display_name": "小明",
  "password": "CorrectHorseBattery1"
}
```

登录成功响应：

```json
{
  "access_token": "eyJ…",
  "token_type": "Bearer",
  "expires_in": 900,
  "user": {
    "id": "…",
    "username": "ming",
    "display_name": "小明",
    "email": "ming@example.com",
    "email_verified": false
  }
}
```

完成已验证的 SMTP/邮件供应商接入后，开启 `CUSTOMER_EMAIL_DELIVERY_ENABLED=true`。届时同一套接口会签发只保存哈希的一次性验证/重置 token，并由 `POST /api/customer/auth/email-verifications`、`POST /api/customer/auth/email-verifications/confirm` 和 `POST /api/customer/auth/password-resets` 完成投递后的流程；在此之前前端不得引导用户等待邮件或暴露调试 token。

## 一、核心问答

### `POST /api/agent/runs`

发送用户咨询并接收渐进式问答结果。系统先完成意图和检索计划，再按公开事件顺序返回证据、回答和最终结构化结果。

请求：

```json
{
  "question": "换季干燥紧绷，预算 600 元"
}
```

字段约束：`question` 为 2～500 字符。

成功响应头：

```text
content-type: text/event-stream; charset=utf-8
```

每个 SSE 消息均为如下 JSON（以空行分隔）：

```text
data: {"type":"...","run_id":"run_...","at":"2026-09-29T...Z","payload":{...}}

```

公共字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | string | 事件类型，见下表 |
| `run_id` | string | 单次问答关联标识，可用于审计查询 |
| `at` | string | 服务端事件时间 |
| `payload` | object | 事件类型对应数据 |

事件顺序与前端处理：

| 事件 | `payload` 关键字段 | 前端动作 |
| --- | --- | --- |
| `run_started` | `summary` | 创建本轮对话的加载状态 |
| `retrieval_plan` | `intent`、`sub_questions`、`sources`、`next_action` | 可选展示“正在理解问题/检索资料”；不要展示内部推理 |
| `tool_started` | `tool`、`summary` | 展示检索进度 |
| `tool_finished` | `tool`、`summary` | 更新检索进度 |
| `evidence_ready` | `evidence`、`summary` | 渲染“依据/资料来源”卡片 |
| `message_delta` | `text`、`summary` | 追加或一次性渲染回答文本 |
| `approval_required` | `actions`、`summary` | 展示“确认加入购物车”卡片；仅开启购物车草稿时出现 |
| `run_completed` | `result`、`summary` | 以最终结果统一落盘和渲染 |
| `run_failed` | `summary`、`fallback_reason`（可选） | 结束加载态，保留用户输入和本轮公开步骤，提供重试入口；绝不展示草稿成功 |

`retrieval_plan.intent` 取值：`recommendation`、`product_knowledge`、`safety`、`clarify`、`out_of_scope`。当为 `safety`、`clarify` 或 `out_of_scope` 时，前端只显示服务端回答和证据（如有），不要强制展示商品推荐。

`evidence` 元素：

```json
{
  "id": "chunk_serum_v21#chunk-1",
  "sku_id": "sku_serum_30",
  "title": "屏障修护精华产品资料",
  "version": "v2.1",
  "quote": "……已审核资料中的相关片段……",
  "retrieval_score": 0.82
}
```

`run_completed.payload.result`：

```json
{
  "answer": "基于已审核资料的选购说明……",
  "query_plan": {
    "intent": "recommendation",
    "sub_questions": ["识别肤况与需求"],
    "sources": ["search_catalog", "get_realtime_price_stock", "retrieve_evidence"],
    "next_action": "answer"
  },
  "model_usage": {
    "model": null,
    "provider_attempted": false,
    "input_tokens": null,
    "output_tokens": null,
    "total_tokens": null,
    "usage_reported": false,
    "elapsed_ms": null,
    "output_token_limit": 300,
    "model_call_count": 0,
    "model_call_budget": 1,
    "evidence_count": 3,
    "evidence_chars": 720,
    "skipped_or_fallback_reason": "未配置生成模型时使用确定性回答"
  },
  "evidence": [],
  "bundles": [],
  "actions": [],
  "requires_confirmation": false
}
```

`bundles` 的套餐结构：

```json
{
  "bundle_id": "bundle_barrier_7d",
  "name": "7 日屏障修护套餐",
  "bundle_price_fen": 52800,
  "list_price_fen": 60700,
  "items": [
    {"sku_id": "sku_cleanser_100", "name": "…", "spec": "…", "price_fen": 12900, "quantity": 1}
  ],
  "routine": ["1 温和洁面", "2 修护精华", "3 保湿面霜"],
  "caution": "首次使用先做局部测试；……"
}
```

当 `requires_confirmation=true` 时，`actions` 中会出现：

```json
{
  "tool": "create_cart_draft",
  "bundle_id": "bundle_barrier_7d",
  "confirmation_token": "confirm_…",
  "requires_confirmation": true,
  "summary": "确认后才会创建购物车草稿；创建前会再次校验价格和库存。"
}
```

前端只临时保存 `confirmation_token`，不得修改或自行生成它。

### Vue 读取 SSE 的最小示例

```ts
export async function runAssistant(question: string, onEvent: (event: any) => void) {
  const response = await fetch('http://127.0.0.1:8000/api/agent/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  })
  if (!response.ok || !response.body) throw new Error('问答请求失败')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const messages = buffer.split('\n\n')
    buffer = messages.pop() ?? ''
    for (const message of messages) {
      if (!message.startsWith('data: ')) continue
      onEvent(JSON.parse(message.slice(6)))
    }
  }
}
```

## 二、商品目录

### `GET /api/catalog/products`

返回当前数据库中已上架商品的公开预览。该接口不会返回成分、使用方法、注意事项或说明书；商品进入推荐和创建草稿前，服务端仍会重新校验库存和上架状态。

响应示例：

```json
[
  {
    "sku_id": "sku_serum_30",
    "name": "澄肌屏障修护精华",
    "spec": "30ml",
    "price_fen": 26800,
    "tags": ["屏障修护"],
    "image_urls": ["/uploads/products/…"]
  }
]
```

### `GET /api/catalog/products/{sku_id}`

需要前台客户 Bearer access token。仅当商品仍上架时返回完整资料，包括商品说明、说明书、成分披露、使用方法与注意事项；未登录为 `401`，下架或不存在为 `404`。

### 商品后台：`/api/v1/catalog`

以下接口使用 OA Bearer access token；`catalog:product:read` 可查看，`catalog:product:write` 可上传、创建和更新。

| 方法与路径 | 用途 | 关键校验 |
| --- | --- | --- |
| `POST /api/v1/catalog/uploads/images` | `multipart/form-data` 上传一张待关联图片 | 仅 JPEG/PNG/WebP、文件头与 MIME 必须一致、最大 5 MiB |
| `POST /api/v1/catalog/products` | 创建商品 | SKU 唯一；价格/库存为非负整数分/件；`image_ids` 必须是未关联上传图片 |
| `GET /api/v1/catalog/products` | 后台商品列表 | 返回图片 URL、说明书、审核/上架状态和 `revision` |
| `GET /api/v1/catalog/products/{id}` | 后台商品详情 | 同上 |
| `PATCH /api/v1/catalog/products/{id}` | 更新商品或图片顺序 | 必须带 `expected_revision`；版本不一致返回 `409` |

创建示例：

```json
{
  "sku_id": "CJ-SERUM-30",
  "title": "澄肌屏障修护精华",
  "spec": "30ml",
  "price_fen": 26800,
  "stock": 42,
  "manual": "避光保存；开封后建议 12 个月内使用。",
  "ingredients": ["甘油", "神经酰胺"],
  "ingredient_disclosure_complete": true,
  "usage": "洁面后取适量均匀涂抹。",
  "cautions": "首次使用先局部测试。",
  "scenario_tags": ["屏障修护", "干燥感"],
  "on_sale": true,
  "assistant_approved": true,
  "image_ids": ["上传图片接口返回的 id"]
}
```

商品后台本身不自动让任意 SKU 进入固定套餐配方；`CATALOG_SOURCE=database` 会将已审核且上架的商品接入 Agent 的候选和实时库存校验。若 SKU 要参与固定套餐，还必须在受审核的套餐配置中出现，不能由模型自行拼装。

## 三、知识库管理与检索

### `GET /api/knowledge/status`

用于后台展示 RAG 当前状态，不包含密钥或请求内容。

```json
{
  "provider": "siliconflow-bge-m3",
  "ready": true,
  "reason": "api_ready_lazy_request",
  "document_count": 4,
  "chunk_count": 4,
  "vectorized_chunk_count": 4
}
```

### `GET /api/knowledge/documents`

返回已上传资料的元数据和审核状态，不返回切块与向量。

### `POST /api/knowledge/documents`

上传或更新知识资料。相同 `document_id` 会完整替换旧版本的切块；仅 `approved=true` 且 `on_sale=true` 的资料可被问答检索。

```json
{
  "document_id": "doc_serum_usage_v3",
  "title": "屏障修护精华使用说明",
  "content": "清洁后取适量均匀涂抹……",
  "version": "v3",
  "sku_ids": ["sku_serum_30"],
  "tags": ["使用方法", "屏障修护"],
  "approved": true,
  "on_sale": true,
  "data_owner": "内容运营"
}
```

字段限制：`document_id` 3～100 字符、`title` 2～200 字符、`content` 1～20,000 字符、`version` 1～50 字符、`data_owner` 2～100 字符。

响应：

```json
{
  "document_id": "doc_serum_usage_v3",
  "version": "v3",
  "chunk_count": 1,
  "searchable": true,
  "message": "已入库并可检索"
}
```

> 当前接口接受文本 JSON，不接受 PDF、Word 或图片文件。文件解析、对象存储和审核流是后续版本能力。

### `POST /api/knowledge/search`

后台调试检索接口；正式用户问答应调用 `/api/agent/runs`。

```json
{
  "question": "屏障修护精华怎么用？",
  "sku_ids": ["sku_serum_30"],
  "limit": 3
}
```

`limit` 默认为 3，范围为 1～10。返回 `Evidence[]`，结构与 `evidence_ready.payload.evidence` 相同。

## 四、确认后创建购物车草稿

### `POST /api/cart-drafts`

仅在问答结果给出 `approval_required` 后调用。前端必须等待用户主动确认，服务端会再次校验确认令牌、商品价格和库存。

```json
{
  "confirmation_token": "confirm_…",
  "confirmed": true
}
```

成功响应：

```json
{
  "cart_draft_id": "cart_draft_…",
  "bundle": {"bundle_id": "bundle_barrier_7d"},
  "status": "created",
  "price_checked_at": "2026-09-29T00:00:00Z"
}
```

业务冲突返回 `409`：包括未确认、令牌无效/过期、商品不可售、库存变化或价格变化。当前 V1 默认不生成确认动作；仅在后端配置 `ENABLE_CART_DRAFTS=true` 时启用。

## 五、调试与审计接口

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/health` | HTTP 存活检查，不能代表模型调用成功 |
| `GET /api/workflows/mermaid` | 返回 `recommendation`、`cart_draft` 两张实际工作流 Mermaid 图 |
| `GET /api/audit-logs` | 助手审计记录，含策略判定与结果；配置 SQLite 后可跨重启查询 |
| `GET /api/model-usage-logs` | 生成模型用量记录；不包含 Prompt 与密钥，配置 SQLite 后可跨重启查询 |

## 六、错误处理与 V1 边界

| 状态码 | 场景 | 前端建议 |
| --- | --- | --- |
| `200` | 普通 JSON 或 SSE 建连成功 | 按协议消费数据 |
| `409` | 购物车确认冲突或库存/价格二次校验失败 | 保留套餐卡，提示刷新后重新确认 |
| `422` | 请求字段缺失、超长或类型错误 | 在表单处提示并禁止提交 |
| `5xx` | 服务端异常 | 显示重试入口；不要把错误内容当作商品事实展示 |

未设置 `SKIN_ASSISTANT_DB_PATH` 时，助手运行状态只保留在内存；设置后 SQLite 会持久化运行轨迹、审计、模型用量、确认令牌和草稿回执。知识资料仍是内存实现，生产接入前仍需要替换为真实商品/库存服务、持久化知识库、身份权限和审核后台。SQLite 不能替代多实例生产的 PostgreSQL，也不能替代商城事实源。
