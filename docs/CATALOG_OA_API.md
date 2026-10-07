# 商品 OA 联调接口

本文档描述商品运营后台使用的接口。所有金额均为人民币**整数分**，所有写操作由服务端按 OA 权限校验并记录审计日志。

## 前置条件

- 服务已执行 `uv run alembic upgrade head`，数据库版本至少为 `20261004_04`；
- 用户先通过 `POST /api/v1/auth/login` 获取短期 access token；
- 请求头携带 `Authorization: Bearer <access_token>`；
- 列表/详情需要 `catalog:product:read`，上传、创建、编辑需要 `catalog:product:write`。`super_admin` 可直接联调。

## 1. 上传商品图片

`POST /api/v1/catalog/uploads/images`

请求为 `multipart/form-data`，字段名为 `file`。仅接受 JPEG、PNG、WebP，单文件最多 5 MiB；服务端会同时校验声明的 MIME 类型和文件内容头，原始文件名不会成为访问路径。

成功响应（`201`）：

```json
{
  "id": "ab12...",
  "url": "/uploads/products/4f2d....png",
  "content_type": "image/png",
  "size_bytes": 182044,
  "sort_order": 0
}
```

将响应中的 `id` 放入下一步商品请求的 `image_ids`；`url` 可直接供图片组件展示。图片先上传、后关联，因此单独上传成功不代表它已出现在任一商品中。

## 2. 创建商品

`POST /api/v1/catalog/products`

```json
{
  "sku_id": "CJ-SERUM-30",
  "title": "澄肌屏障修护精华",
  "spec": "30ml",
  "price_fen": 26800,
  "stock": 42,
  "description": "适用于日常保湿修护。",
  "manual": "避光保存；开封后建议 12 个月内使用。",
  "ingredients": ["甘油", "神经酰胺"],
  "ingredient_disclosure_complete": true,
  "usage": "洁面后取适量均匀涂抹。",
  "cautions": "首次使用先局部测试。",
  "category_code": "FACE_SERUM",
  "scenario_tags": ["屏障修护", "干燥感"],
  "on_sale": true,
  "assistant_approved": true,
  "image_ids": ["ab12..."]
}
```

约束：SKU 为 3～64 位字母、数字、`_` 或 `-` 且全局唯一；价格和库存均不可为负；成分、场景标签和图片 ID 不可重复；一张图片不能被关联到多个商品。

## 3. 查询商品

| 接口 | 说明 |
| --- | --- |
| `GET /api/v1/catalog/products` | 返回全部后台商品，含未上架、未审核和图片元数据。 |
| `GET /api/v1/catalog/products/{product_id}` | 返回单个商品详情。 |

成功响应包含 `revision`。编辑前必须保留此字段，以防覆盖其他运营人员已提交的修改。

## 4. 更新商品

`PATCH /api/v1/catalog/products/{product_id}`

仅提交要修改的字段，并必须提交当前版本：

```json
{
  "stock": 18,
  "on_sale": true,
  "image_ids": ["ab12...", "cd34..."],
  "expected_revision": 3
}
```

成功时版本号加一。若版本已经变化，接口返回 `409 product revision conflict`；页面应刷新数据后由运营人员确认再提交，而不是自动覆盖。

## 5. Agent 接入边界

默认 `CATALOG_SOURCE=mock`，商品后台录入不会自动替换既有演练商品。商品审核、上架且资料完整后，显式设置 `CATALOG_SOURCE=database` 并重启后端，公开目录、Agent 候选检索与确认前库存/价格复核才会读取后台商品。数据库读取失败会报错，绝不静默回退到 Mock。

Agent 的固定套餐仍需受审核的套餐配置；模型不会仅因新建了 SKU 就自行拼装套餐。真实商城库存、订单与支付仍应通过权威商城服务完成。

## 常见响应

| 状态码 | 含义与前端处理 |
| --- | --- |
| `401` | 未登录或 access token 失效，走已有 refresh/login 流程。 |
| `403` | 当前 OA 账号缺少商品读写权限，隐藏写入操作并提示联系管理员授权。 |
| `409` | SKU 重复、图片已关联，或编辑版本冲突；刷新商品后人工确认。 |
| `413` | 图片超过 5 MiB；提示压缩后重试。 |
| `415` | 图片格式不支持；仅允许 JPEG、PNG、WebP。 |
| `422` | 字段、图片内容或图片 ID 校验失败；保留表单内容并定位对应字段。 |
