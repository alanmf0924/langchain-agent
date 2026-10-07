# 商城商品与库存读模型接入契约（待确认）

> 状态：**D1 已按默认方案执行，商城接口尚未接入**。金额已在当前前后端契约中迁移为整数分；其余接入决策仍待真实环境信息确认。

## 1. 目标、角色与不可逾越的边界

**用户目标**：获得可售商品推荐；确认创建草稿前，系统再次获取当前可售性与价格。

| 角色/系统 | 负责内容 | 不负责内容 |
| --- | --- | --- |
| 护肤助手 | 意图路由、候选排序、证据呈现、确认、审计、SSE | 决定或写入价格、库存、上下架状态 |
| 商城商品服务 | SKU/SPU、展示信息、上架、可推荐资格、标价 | 生成模型答案、护肤诊断 |
| 商城库存服务 | 指定 SKU 的当前可售性、可售数量/预占资格、库存时间戳 | 向模型解释商品功效 |
| 内容审核服务 | 资料审核状态、版本与责任人 | 价格、库存和交易授权 |
| 用户 | 审阅推荐资料并确认“创建草稿” | 伪造价格、库存、确认令牌 |

RAG、模型输出和前端缓存均不能作为价格、库存、上架或授权事实。服务不可用、数据过期或版本冲突时，助手必须停止创建草稿，不以旧数据补全。

## 2. 推荐的服务契约

推荐提供两个**只读、面向服务间调用**的接口。接口名可按商城既有规范调整，但字段语义、错误约定和 freshness 约束不应改变。

### 2.1 发现候选商品

`POST /internal/assistant/v1/catalog/candidates:search`

请求由护肤助手的确定性 QueryPlan 产生，不发送用户完整原文：

```json
{
  "request_id": "run_...",
  "scenario_tags": ["屏障修护", "干燥感", "紧绷感"],
  "category_codes": ["FACE_CLEANSER", "FACE_SERUM", "FACE_CREAM"],
  "eligible_for_assistant": true,
  "limit": 20
}
```

响应：

```json
{
  "source": "catalog-service",
  "source_version": "2026-10-01T09:00:00Z",
  "retrieved_at": "2026-10-01T09:00:03Z",
  "items": [
    {
      "sku_id": "TST-SKIN-CLEAN-100",
      "spu_id": "TST-SPU-CLEAN",
      "name": "测试温和洁面",
      "spec": "100ml",
      "category_code": "FACE_CLEANSER",
      "scenario_tags": ["温和清洁", "干燥感"],
      "assistant_approved": true,
      "on_sale": true,
      "sale_price_fen": 12900,
      "price_version": "price_20261001_01",
      "product_updated_at": "2026-10-01T09:00:00Z"
    }
  ]
}
```

约束：

- `sale_price_fen` 必须为非负整数；商城和助手之间统一使用“分”，前端再格式化为人民币。现有后端内部 `price` 为整元，需要在接入适配器时迁移为 `price_fen`，不得用浮点数。
- `assistant_approved=true`、`on_sale=true` 才可成为候选；缺少任何必填字段的项目整体拒绝，不推荐部分不完整数据。
- 返回的 `source`、`source_version`、`retrieved_at` 必须被审计，但不直接写给模型。

### 2.2 批量读取可售价格与库存（推荐前、确认前）

`POST /internal/assistant/v1/catalog/availability:batch`

```json
{
  "request_id": "run_...",
  "purpose": "recommendation",
  "consistency": "bounded_staleness",
  "max_age_seconds": 30,
  "items": [
    {"sku_id": "TST-SKIN-CLEAN-100", "quantity": 1},
    {"sku_id": "TST-SKIN-SERUM-30", "quantity": 1}
  ]
}
```

确认前必须改为：

```json
{
  "request_id": "cart_...",
  "purpose": "cart_draft_confirmation",
  "consistency": "strong",
  "max_age_seconds": 5,
  "items": [{"sku_id": "TST-SKIN-CLEAN-100", "quantity": 1}]
}
```

响应：

```json
{
  "source": "inventory-service",
  "retrieved_at": "2026-10-01T09:01:03Z",
  "items": [
    {
      "sku_id": "TST-SKIN-CLEAN-100",
      "requested_quantity": 1,
      "is_sellable": true,
      "available_quantity": 42,
      "on_sale": true,
      "assistant_approved": true,
      "sale_price_fen": 12900,
      "price_version": "price_20261001_01",
      "inventory_version": "inv_20261001_912",
      "inventory_checked_at": "2026-10-01T09:01:02Z",
      "product": {
        "name": "测试温和洁面",
        "spec": "100ml",
        "scenario_tags": ["温和清洁", "干燥感"],
        "ingredients": ["测试成分"],
        "ingredient_disclosure_complete": true,
        "usage": "测试用法。",
        "cautions": "测试注意事项。"
      }
    }
  ]
}
```

约束：

- 后端仅将 `is_sellable`、价格、上架与审核结果提供给消费者页面；`available_quantity` 是服务端审计/判断字段，默认不在 UI 展示。
- `product` 是确认后重启恢复所需的最小展示快照。它只包含名称、规格、标签、成分、用法和注意事项；SKU、价格、上架和审核状态以同层实时字段为准。
- 当用户要求避开香精、酒精或精油时，只有 `ingredient_disclosure_complete=true` 的完整成分表才可用于判断。缺失、`false` 或空成分表必须返回“资料不足”，不得将“未列出”推断为“不含”。
- 批量响应必须逐项返回。遗漏 SKU、重复 SKU、数量不匹配或 `inventory_checked_at` 超过 `max_age_seconds`，整个套餐视为不可核验，不能创建草稿。
- 该调用不扣减库存、不预占库存。若商城要求预占，必须新建独立的、显式确认后的写接口，不能复用本读接口。

## 3. 测试环境鉴权与最小数据权限

### 默认方案（建议确认）

采用 OAuth 2.0 Client Credentials，测试与生产使用不同 issuer、client 和 audience：

| 项目 | 约定 |
| --- | --- |
| 调用方身份 | `skin-assistant-backend` 服务账号，不使用用户 Cookie 或个人 API Key |
| Token | `Authorization: Bearer <access_token>`，由部署平台/Secret Manager 注入，仅在内存使用 |
| Scope | `catalog.assistant.read`、`inventory.assistant.read` |
| 关联 ID | 每次请求传 `X-Request-ID`（运行 ID 或草稿确认 ID），不传用户原文、手机号或地址 |
| 网络 | 测试环境私网/网关 allowlist；生产额外使用 mTLS（若商城已有服务网格则沿用） |
| 密钥 | 仅保存于 Secret Manager 或忽略的本地 `.env`；不写入 Git、SSE、审计正文或评测文件 |

`401/403` 是配置/权限故障：助手记录脱敏错误码并向用户显示“暂时无法核验商品信息”，不降级为 Mock。`429/503/超时` 是可恢复依赖故障：可重试一次（指数退避，总时限 2 秒）；仍失败则结束本轮或保留推荐说明但移除草稿确认能力。

## 4. 脱敏测试 SKU 与固定场景

以下是**建议在测试环境创建的合成 SKU 别名**；不对应真实品牌、用户、订单或生产库存。请由商城团队映射为实际测试数据后回填最终 SKU。

| 别名 | 场景 | 必需状态 | 预期结果 |
| --- | --- | --- | --- |
| `TST-SKIN-CLEAN-100` | 修护套餐洁面 | 可售、审核通过 | 正常推荐/确认 |
| `TST-SKIN-SERUM-30` | 修护套餐精华 | 可售、审核通过 | 正常推荐/确认 |
| `TST-SKIN-CREAM-50` | 修护套餐面霜 | 可售、审核通过 | 正常推荐/确认 |
| `TST-SKIN-OIL-CLEAN-120` | 控油套餐洁面 | 可售、审核通过 | 控油推荐 |
| `TST-SKIN-OIL-SERUM-30` | 控油套餐精华 | 可售、审核通过 | 控油推荐 |
| `TST-SKIN-OIL-LOTION-50` | 控油套餐乳液 | 可售、审核通过 | 控油推荐 |
| `TST-SKIN-OOS-30` | 缺货反例 | `is_sellable=false` | 不进入套餐 |
| `TST-SKIN-OFFSALE-30` | 下架反例 | `on_sale=false` | 不进入套餐 |
| `TST-SKIN-UNAPPROVED-30` | 未审核反例 | `assistant_approved=false` | 不进入推荐或模型证据 |
| `TST-SKIN-PRICE-CHANGE-30` | 确认前改价 | `price_version` 变化 | 拒绝旧套餐，要求重新咨询 |

测试数据仅保留合成商品资料；不得复制生产用户、订单、地址、真实库存总量或内部成本价。测试帐号只允许访问这些 `TST-*` SKU。

## 5. 数据刷新、陈旧与冲突处理

| 数据 | 权威方 | 推荐阶段最大陈旧度 | 确认阶段最大陈旧度 | 更新机制 | 不满足时的行为 |
| --- | --- | ---: | ---: | --- | --- |
| 上架/助手审核资格 | 商品/内容服务 | 5 分钟 | 5 秒 | 发布事件 + 强校验读取 | 不推荐/不创建草稿 |
| 售价与 `price_version` | 价格服务 | 30 秒 | 5 秒 | 价格变更事件 + 读取 | 旧套餐失效，重新推荐 |
| 可售库存与 `inventory_version` | 库存服务 | 30 秒 | 5 秒 | 库存读取；不以本地缓存确认 | 不创建草稿，提示刷新 |
| 商品展示属性与 tags | 商品服务 | 5 分钟 | 不单独依赖 | 发布事件或 5 分钟轮询 | 候选不足则不推荐 |
| 审核资料版本 | 内容审核服务 | 发布版本 | 确认不依赖 | 审核发布事件 | 已撤回资料不得继续检索 |

缓存只可用于减少“推荐阶段”的读取；`cart_draft_confirmation` 必须绕过助手缓存并要求商城返回时间在 5 秒内。若价格、库存或资格任一版本相较推荐快照变化，当前令牌失效，用户可保留原问题重新发起咨询。

## 6. 失败状态与前端可见行为

| 条件 | SSE/接口行为 | 用户可见文案方向 | 持久化与恢复 |
| --- | --- | --- | --- |
| 候选为空 | `run_completed`，无套餐 | “暂无具备审核资料的可售匹配商品” | 保存 trace，可补充诉求 |
| 推荐阶段商城超时 | `run_failed` 或完成但无草稿动作 | “暂时无法核验商品信息，请稍后重试” | 保存脱敏错误类别、request_id |
| 确认前缺货/下架 | `POST /cart-drafts` 返回 `409` | “商品状态已变化，请重新核验” | 令牌不消费；记录失败审计 |
| 确认前价格变化 | `409` | “价格已变化，需重新生成套餐” | 当前令牌作废；保留历史推荐 trace |
| 鉴权失败 | 无 Mock 回退 | “当前服务配置未就绪” | 告警给服务负责人，不泄露详情 |
| 单项 SKU 数据缺失 | 整个套餐不可确认 | “套餐信息不完整，暂不能创建草稿” | 审计缺失 SKU 与 source 版本 |

## 7. 可验收标准

| ID | 前置条件 | 操作 | 可观察结果 |
| --- | --- | --- | --- |
| AC-01 | 三个 `TST-SKIN-*` 修护 SKU 均可售 | 发起修护咨询 | 推荐只引用审核资料，返回带 `price_version`/来源时间的服务端快照 |
| AC-02 | 推荐后把一个 SKU 调为缺货 | 用户确认草稿 | 返回 `409`，无草稿成功回执、令牌未被成功消费 |
| AC-03 | 推荐后修改价格版本 | 用户确认草稿 | 返回 `409`，提示重新咨询；不展示旧价格成功状态 |
| AC-04 | 库存服务返回超过 5 秒的确认快照 | 用户确认草稿 | 拒绝创建草稿；审计记录陈旧原因 |
| AC-05 | 测试服务账号缺 scope | 发起查询 | 不使用 Mock，不泄露 token/后端详情，产生可定位 request_id |
| AC-06 | 同一令牌连续提交两次 | 两次确认请求 | 最多一个草稿成功；草稿与成功审计一一对应 |

## 8. 请确认的决策

回复“按默认方案确认”即可进入适配器实现；若有不同约束，只需标明编号和替换值。

| 编号 | 默认建议 | 需要确认 |
| --- | --- | --- |
| D1 | 金额统一用整数分 `sale_price_fen`，已迁移现有整元 `price` 字段 | 已执行 |
| D2 | OAuth2 Client Credentials + 两个 read scopes；生产加 mTLS | 是/否；若已有网关鉴权，提供其文档/字段 |
| D3 | 推荐读取最大陈旧 30 秒；确认强校验最大 5 秒 | 是/否；若不同，给出 SLA |
| D4 | 测试仅创建/映射 `TST-SKIN-*` 合成 SKU，不访问生产数据 | 是/否；提供测试环境与测试 SKU 清单 |
| D5 | 价格、库存、上架、审核任一变更均使当前确认令牌失效 | 是/否；若价格变动允许继续，请提供容忍阈值和用户告知规则 |
| D6 | 外部依赖超时、401/403 或不完整响应时，不回退 Mock，不创建草稿 | 是/否 |
