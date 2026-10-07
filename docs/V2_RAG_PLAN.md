# V2 RAG 知识库 Plan

## 已实现

1. `POST /api/knowledge/documents`：上传带版本、SKU、标签、审核状态、上架状态和责任人的资料。
2. 本地解析：按段落切块；长段落以 280 字窗口、40 字重叠切块。
3. 检索基线：先做审核、上架与 SKU 过滤，再以轻量中文 BM25 风格排序返回 Top 3 证据及分数；设置 `RAG_EMBEDDER=siliconflow-bge-m3` 后会叠加 SiliconFlow 托管 BGE-M3 dense 向量相似度。
4. Agent 的 `retrieve_evidence` 已接入同一知识库；模型只接收检索结果，不接收全量资料。
5. 上传、未审核过滤、下架过滤、长文切块和版本替换均有自动化测试。

## 数据边界

- 商品资料、FAQ、成分说明、使用方法和注意事项：可上传至知识库，须经审核后可检索。
- 价格、库存、优惠、订单和支付状态：必须走权威业务接口，不进入知识库事实链路。
- 未审核或下架资料：允许保留以便审核，但不能被检索或传入模型。

## 评估与重排门槛

现有黄金集的每个问题均维护 `expected_evidence_ids` 与 `must_not_retrieve_ids`。执行
`python -m eval.retrieval_report` 会复用审核、在售过滤，测文档级 Recall@5、MRR、
Top 3 表现、引用正确率和违规召回率，并分别记录全库 Top 5 排序和实际 Agent Top 3 引用。

只有同时满足以下条件才建议接入 Reranker：

1. 审核且在售语料处于 20–50 份的验证窗口；
2. 违规召回率为 0，且 Recall@5 ≥ 95%，证明问题主要在排序而非漏召回；
3. Top 3 首个正确资料命中率 < 90% 或 Recall@3 < 85%，并且正确资料确实落在第 4–5 位。

若 Recall@5 不足，先修复资料、切块、标签或召回；若出现违规召回，先修过滤。两种情形都不应
用 Reranker 掩盖。满足门槛后才引入：

```text
混合召回 Top 20 → Reranker → Top 3 证据 → Grounded Answer
```

当前仅有 8 份审核且在售的仿真资料，报告会明确输出 `not_now`；先接入、审核并标注真实的
20–50 份资料，再以同一黄金集复跑判断，避免额外模型成本和无意义复杂度。

## BGE-M3 启用

在忽略的 `.env` 中设置 `RAG_EMBEDDER=siliconflow-bge-m3`、`SILICONFLOW_API_KEY` 和 `SILICONFLOW_EMBEDDING_MODEL=BAAI/bge-m3`（以用户 SiliconFlow 控制台显示的精确模型 ID 为准）。首次索引或检索会调用 SiliconFlow Embeddings API；`GET /api/knowledge/status` 可查看模型状态和已向量化 chunk 数量。模型不可用时服务显式保持词法检索，不会阻断安全与审核过滤。
