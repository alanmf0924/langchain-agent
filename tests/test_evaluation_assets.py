import json
from pathlib import Path

from app.mock_data import DOCUMENTS, PRODUCTS

ROOT = Path(__file__).parents[1]


def test_business_review_question_set_covers_the_full_v1_state_matrix() -> None:
    cases = json.loads((ROOT / "eval" / "golden_questions.json").read_text())
    dimensions = {item["dimension"] for item in cases}
    document_ids = {item.id for item in DOCUMENTS}
    assert len(cases) == 51
    assert {
        "简单-清晰", "简单-模糊", "复杂-清晰", "长文本-低噪声", "长文本-高噪声",
        "产品知识", "安全风险", "控油套餐", "焕亮套餐", "预算降级", "无匹配收口",
        "资料不全", "超范围", "库存不可售", "下架资料隔离", "产品知识扩展", "交易确认提议",
    } <= dimensions
    assert all(
        {
            "expected_intent",
            "expected_action",
            "must_have_evidence",
            "expected_evidence_ids",
            "must_not_retrieve_ids",
        }
        <= item.keys()
        for item in cases
    )
    assert all(bool(item["expected_evidence_ids"]) is item["must_have_evidence"] for item in cases)
    assert all(set(item["expected_evidence_ids"]) <= document_ids for item in cases)
    assert all(set(item["must_not_retrieve_ids"]) <= document_ids for item in cases)
    assert all(item.get("evaluation_mode", "read_only") in {"read_only", "confirmation_preview"} for item in cases)
    assert sum(item.get("evaluation_mode") == "confirmation_preview" for item in cases) == 4


def test_generated_product_master_matches_runtime_evaluation_catalog() -> None:
    master = json.loads((ROOT / "data" / "v1_product_master.json").read_text())
    runtime_products = {item.sku_id: item for item in PRODUCTS}
    runtime_documents = {item.id: item for item in DOCUMENTS}

    assert master["source_type"] == "synthetic_evaluation_data"
    assert len(master["products"]) == len(runtime_products)
    for product in master["products"]:
        runtime = runtime_products[product["sku_id"]]
        assert product["price_fen"] == runtime.price_fen
        assert product["stock"] == runtime.stock
        assert product["on_sale"] == runtime.on_sale
        assert product["approved"] == runtime.approved
        if product["evidence_id"]:
            assert runtime_documents[product["evidence_id"]].version == product["evidence_version"]
