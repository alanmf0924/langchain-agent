from app.knowledge_base import MAX_CHUNK_CHARS, KnowledgeBase
from app.mock_data import DOCUMENTS
from app.models import KnowledgeDocumentUpload


class FakeDenseEmbedder:
    name = "bge-m3"

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            if "胆固醇" in text:
                vectors.append([0.0, 1.0])
            elif "氨基酸" in text:
                vectors.append([0.0, 0.0])
            else:
                vectors.append([1.0, 0.0])
        return vectors

    def status(self) -> dict[str, str | bool]:
        return {"provider": self.name, "ready": True, "reason": "loaded"}


def test_seeded_retrieval_returns_only_grounded_on_sale_evidence() -> None:
    knowledge_base = KnowledgeBase(DOCUMENTS)
    results = knowledge_base.search("换季干燥紧绷，想做屏障修护", ["sku_serum_30"], limit=3)
    assert [item.id for item in results] == ["chunk_serum_v21"]
    assert results[0].retrieval_score and results[0].retrieval_score > 0


def test_off_sale_document_is_not_retrievable_even_when_its_words_match() -> None:
    knowledge_base = KnowledgeBase(DOCUMENTS)
    results = knowledge_base.search("焕亮精华怎么用", ["sku_offline_30"], limit=3)
    assert results == []


def test_unapproved_upload_is_stored_but_never_searchable() -> None:
    knowledge_base = KnowledgeBase(DOCUMENTS)
    receipt = knowledge_base.ingest(
        KnowledgeDocumentUpload(
            document_id="doc_pending_review",
            title="待审核舒缓资料",
            content="这是待审核的舒缓资料，不应被问答检索。",
            version="v0.1",
            sku_ids=["sku_serum_30"],
            tags=["舒缓"],
            approved=False,
            on_sale=True,
            data_owner="内容运营",
        )
    )
    assert receipt.searchable is False
    assert all(item.id != "doc_pending_review" for item in knowledge_base.search("舒缓", [], limit=10))


def test_upload_chunks_long_content_and_replaces_old_version() -> None:
    knowledge_base = KnowledgeBase(DOCUMENTS)
    content = "保湿修护资料。" * (MAX_CHUNK_CHARS // 3 + 10)
    first = knowledge_base.ingest(
        KnowledgeDocumentUpload(
            document_id="doc_barrier_faq",
            title="屏障修护 FAQ",
            content=content,
            version="v1",
            sku_ids=["sku_serum_30"],
            tags=["屏障修护"],
            approved=True,
            on_sale=True,
            data_owner="内容运营",
        )
    )
    second = knowledge_base.ingest(
        KnowledgeDocumentUpload(
            document_id="doc_barrier_faq",
            title="屏障修护 FAQ",
            content="修订后的屏障修护资料。",
            version="v2",
            sku_ids=["sku_serum_30"],
            tags=["屏障修护"],
            approved=True,
            on_sale=True,
            data_owner="内容运营",
        )
    )
    assert first.chunk_count > 1
    assert second.chunk_count == 1
    results = knowledge_base.search("屏障修护", ["sku_serum_30"], limit=10)
    assert [item.version for item in results if item.id == "doc_barrier_faq"] == ["v2"]


def test_dense_provider_is_fused_with_lexical_baseline_and_exposed_in_status() -> None:
    knowledge_base = KnowledgeBase(DOCUMENTS, embedder=FakeDenseEmbedder())
    results = knowledge_base.search("语义查询", [], limit=3)
    status = knowledge_base.status()
    assert results[0].id == "chunk_serum_v21"
    assert status.provider == "bge-m3"
    assert status.ready is True
    assert status.vectorized_chunk_count == len(DOCUMENTS)
