from types import SimpleNamespace

from app.embeddings import SiliconFlowBgeM3Embedder, build_embedding_provider


class FakeEmbeddingsClient:
    def __init__(self) -> None:
        self.request: dict | None = None

    def create(self, **kwargs):
        self.request = kwargs
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=1, embedding=[0.2, 0.8]),
                SimpleNamespace(index=0, embedding=[0.8, 0.2]),
            ]
        )


def test_siliconflow_bge_m3_uses_openai_compatible_embeddings_api(monkeypatch) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key")
    monkeypatch.setenv("SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3")
    provider = SiliconFlowBgeM3Embedder()
    fake_embeddings = FakeEmbeddingsClient()
    provider._client = SimpleNamespace(embeddings=fake_embeddings)

    vectors = provider.embed(["第一段资料", "第二段资料"])

    assert fake_embeddings.request == {
        "model": "BAAI/bge-m3",
        "input": ["第一段资料", "第二段资料"],
        "encoding_format": "float",
    }
    assert vectors == [[0.8, 0.2], [0.2, 0.8]]
    assert provider.status()["ready"] is True


def test_embedding_provider_only_enables_siliconflow_when_explicit(monkeypatch) -> None:
    monkeypatch.delenv("RAG_EMBEDDER", raising=False)
    assert build_embedding_provider().name == "lexical"
    monkeypatch.setenv("RAG_EMBEDDER", "siliconflow-bge-m3")
    assert build_embedding_provider().name == "siliconflow-bge-m3"
