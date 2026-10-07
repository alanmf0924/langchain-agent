# 检索基线与 Reranker 准入报告

- 生成时间：2026-09-30T23:38:20.521227+00:00
- 执行模式：`deterministic_local_no_external_model_or_transaction_write`
- 语料：共 9 份，其中审核且在售 8 份；全局过滤资料：chunk_offline_v10。
- 评测范围：20/30 条需证据黄金题。
- 排序范围：审核且在售的全库文档级 Top 5；不混入候选 SKU 业务过滤。
- 引用范围：实际 Agent 工作流的 Top 3 引用，保留候选 SKU、审核和在售过滤。

## 指标

| 指标 | 值 |
| --- | ---: |
| Recall@5 | 93.33% |
| MRR | 1.0000 |
| Recall@3 | 81.67% |
| Top 3 首个正确结果命中率 | 100.00% |
| 第 4–5 位可被重排挽回率 | 0.00% |
| 引用正确率 | 79.66% |
| 违规召回率 | 0.00% |
| 违规题目占比 | 0.00% |

## Reranker 决策

- 结论：**暂不接入**（`not_now`）
- 原因：可检索语料为 8 份，不在 20–50 份的验证窗口；先接入并审核真实资料，再测排序收益。
- 门槛：审核且在售语料 20–50 份；Recall@5 ≥ 95%；Top 3 首个正确结果命中率 ≥ 90%；Recall@3 ≥ 85%。

## 逐题检索明细

| 题目 | 预期证据 | Top 5 | 实际引用 Top 3 | R@5 | RR | 违规 |
| --- | --- | --- | --- | ---: | ---: | --- |
| simple-01 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_serum_v21、chunk_cleanser_v18、chunk_cream_v13、chunk_bright_cream_v10 | chunk_serum_v21、chunk_cleanser_v18、chunk_cream_v13 | 100.00% | 1.0000 | 无 |
| simple-02 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_cream_v13、chunk_bright_cream_v10、chunk_serum_v21、chunk_oil_lotion_v10、chunk_oil_serum_v12 | chunk_cream_v13、chunk_serum_v21、chunk_cleanser_v18 | 66.67% | 1.0000 | 无 |
| simple-03 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_cleanser_v18、chunk_serum_v21、chunk_cream_v13、chunk_bright_cream_v10、chunk_oil_cleanser_v11 | chunk_cleanser_v18、chunk_serum_v21、chunk_cream_v13 | 100.00% | 1.0000 | 无 |
| simple-04 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_cream_v13、chunk_bright_cream_v10、chunk_cleanser_v18、chunk_oil_lotion_v10、chunk_serum_v21 | chunk_cream_v13、chunk_cleanser_v18、chunk_serum_v21 | 100.00% | 1.0000 | 无 |
| simple-05 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_serum_v21、chunk_cream_v13、chunk_bright_cream_v10、chunk_cleanser_v18 | chunk_serum_v21、chunk_cream_v13、chunk_cleanser_v18 | 100.00% | 1.0000 | 无 |
| complex-01 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_cleanser_v18、chunk_oil_cleanser_v11、chunk_serum_v21、chunk_cream_v13、chunk_oil_lotion_v10 | chunk_oil_cleanser_v11、chunk_oil_lotion_v10、chunk_oil_serum_v12 | 100.00% | 1.0000 | 无 |
| complex-02 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_serum_v21、chunk_cleanser_v18、chunk_cream_v13、chunk_bright_cream_v10、chunk_oil_lotion_v10 | chunk_serum_v21、chunk_cream_v13、chunk_cleanser_v18 | 100.00% | 1.0000 | 无 |
| complex-03 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_cream_v13、chunk_bright_cream_v10、chunk_oil_lotion_v10、chunk_cleanser_v18、chunk_serum_v21 | chunk_cream_v13、chunk_cleanser_v18、chunk_serum_v21 | 100.00% | 1.0000 | 无 |
| complex-04 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_cream_v13、chunk_bright_cream_v10、chunk_serum_v21、chunk_oil_lotion_v10、chunk_oil_serum_v12 | chunk_cream_v13、chunk_serum_v21、chunk_cleanser_v18 | 66.67% | 1.0000 | 无 |
| complex-05 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_serum_v21、chunk_cleanser_v18、chunk_cream_v13、chunk_bright_cream_v10、chunk_bright_serum_v14 | chunk_serum_v21、chunk_cleanser_v18、chunk_cream_v13 | 100.00% | 1.0000 | 无 |
| noise-01 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_cream_v13、chunk_bright_cream_v10、chunk_serum_v21、chunk_oil_lotion_v10、chunk_cleanser_v18 | chunk_cream_v13、chunk_serum_v21、chunk_cleanser_v18 | 100.00% | 1.0000 | 无 |
| noise-02 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_cream_v13、chunk_bright_cream_v10、chunk_serum_v21、chunk_oil_lotion_v10、chunk_oil_serum_v12 | chunk_cream_v13、chunk_serum_v21、chunk_cleanser_v18 | 66.67% | 1.0000 | 无 |
| noise-03 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_cleanser_v18、chunk_serum_v21、chunk_cream_v13、chunk_oil_cleanser_v11、chunk_bright_cream_v10 | chunk_cleanser_v18、chunk_serum_v21、chunk_cream_v13 | 100.00% | 1.0000 | 无 |
| noise-04 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_cream_v13、chunk_bright_cream_v10、chunk_cleanser_v18、chunk_serum_v21、chunk_oil_lotion_v10 | chunk_cream_v13、chunk_cleanser_v18、chunk_serum_v21 | 100.00% | 1.0000 | 无 |
| noise-05 | chunk_cleanser_v18、chunk_cream_v13、chunk_serum_v21 | chunk_serum_v21、chunk_cream_v13、chunk_bright_cream_v10、chunk_bright_serum_v14、chunk_oil_serum_v12 | chunk_serum_v21、chunk_cream_v13、chunk_cleanser_v18 | 66.67% | 1.0000 | 无 |
| knowledge-01 | chunk_serum_v21 | chunk_serum_v21、chunk_cream_v13、chunk_bright_cream_v10、chunk_oil_serum_v12、chunk_bright_serum_v14 | chunk_serum_v21、chunk_cream_v13、chunk_cleanser_v18 | 100.00% | 1.0000 | 无 |
| knowledge-02 | chunk_cream_v13 | chunk_cream_v13、chunk_bright_cream_v10、chunk_bright_serum_v14、chunk_serum_v21、chunk_oil_serum_v12 | chunk_cream_v13、chunk_cleanser_v18、chunk_oil_cleanser_v11 | 100.00% | 1.0000 | 无 |
| knowledge-03 | chunk_cleanser_v18 | chunk_cleanser_v18、chunk_oil_cleanser_v11、chunk_oil_lotion_v10、chunk_oil_serum_v12、chunk_serum_v21 | chunk_cleanser_v18、chunk_oil_cleanser_v11 | 100.00% | 1.0000 | 无 |
| knowledge-04 | chunk_serum_v21 | chunk_serum_v21、chunk_cream_v13、chunk_bright_serum_v14、chunk_bright_cream_v10、chunk_oil_serum_v12 | chunk_serum_v21、chunk_cream_v13、chunk_cleanser_v18 | 100.00% | 1.0000 | 无 |
| knowledge-05 | chunk_cream_v13 | chunk_cream_v13、chunk_serum_v21、chunk_cleanser_v18、chunk_bright_cream_v10、chunk_bright_serum_v14 | chunk_cream_v13、chunk_serum_v21、chunk_cleanser_v18 | 100.00% | 1.0000 | 无 |
