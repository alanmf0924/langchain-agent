from fastapi.testclient import TestClient

from app.main import app


def test_knowledge_upload_and_search_endpoints_apply_approval_filter(oa_operations_access) -> None:
    client = TestClient(app)
    status = client.get("/api/knowledge/status")
    assert status.status_code == 200
    assert status.json()["provider"] == "lexical"
    assert status.json()["vectorized_chunk_count"] == 0
    upload = client.post(
        "/api/knowledge/documents",
        json={
            "document_id": "doc_api_test_approved",
            "title": "API 修护资料",
            "content": "修护精华建议在洁面后使用，首次使用应局部测试。",
            "version": "v1",
            "sku_ids": ["sku_serum_30"],
            "tags": ["屏障修护"],
            "approved": True,
            "on_sale": True,
            "data_owner": "内容运营",
        },
    )
    assert upload.status_code == 200
    assert upload.json()["searchable"] is True

    results = client.post(
        "/api/knowledge/search",
        json={"question": "屏障修护精华怎么用", "sku_ids": ["sku_serum_30"]},
    )
    assert results.status_code == 200
    assert any(item["id"] == "doc_api_test_approved" for item in results.json())

    pending = client.post(
        "/api/knowledge/documents",
        json={
            "document_id": "doc_api_test_pending",
            "title": "待审核资料",
            "content": "待审核资料不得被用户问答检索。",
            "version": "v1",
            "sku_ids": ["sku_serum_30"],
            "tags": ["修护"],
            "approved": False,
            "on_sale": True,
            "data_owner": "内容运营",
        },
    )
    assert pending.status_code == 200
    assert pending.json()["searchable"] is False

    all_results = client.post(
        "/api/knowledge/search",
        json={"question": "待审核资料", "sku_ids": ["sku_serum_30"], "limit": 10},
    )
    assert all_results.status_code == 200
    assert all(item["id"] != "doc_api_test_pending" for item in all_results.json())
