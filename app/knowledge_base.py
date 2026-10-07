"""V2 最小知识库：上传、版本化切块、审核过滤与可解释的检索基线。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import log

from app.embeddings import EmbeddingProvider, LexicalOnlyEmbedder
from app.models import (
    DocumentChunk,
    Evidence,
    KnowledgeBaseStatus,
    KnowledgeDocumentReceipt,
    KnowledgeDocumentUpload,
)

MAX_CHUNK_CHARS = 280
CHUNK_OVERLAP_CHARS = 40


@dataclass(frozen=True)
class KnowledgeChunk:
    id: str
    document_id: str
    sku_ids: tuple[str, ...]
    title: str
    version: str
    content: str
    tags: tuple[str, ...]
    approved: bool
    on_sale: bool
    data_owner: str
    dense_vector: tuple[float, ...]


def _terms(text: str) -> list[str]:
    """轻量中文检索分词：单字、双字和英文词并用，避免依赖未验证的外部模型。"""
    compact = re.sub(r"\s+", "", text.lower())
    chinese = re.findall(r"[\u4e00-\u9fff]", compact)
    pairs = ["".join(chinese[index : index + 2]) for index in range(len(chinese) - 1)]
    english = re.findall(r"[a-z0-9]+", compact)
    return chinese + pairs + english


def _chunk_text(content: str) -> list[str]:
    """优先按段落切块，长段落才使用固定窗口，避免打散短 FAQ。"""
    paragraphs = [item.strip() for item in re.split(r"\n{2,}", content) if item.strip()]
    chunks: list[str] = []
    for paragraph in paragraphs or [content.strip()]:
        if len(paragraph) <= MAX_CHUNK_CHARS:
            chunks.append(paragraph)
            continue
        start = 0
        while start < len(paragraph):
            end = min(start + MAX_CHUNK_CHARS, len(paragraph))
            chunks.append(paragraph[start:end])
            if end == len(paragraph):
                break
            start = end - CHUNK_OVERLAP_CHARS
    return chunks


class KnowledgeBase:
    """进程内知识库，接口可在下一阶段替换为 Qdrant/对象存储适配器。"""

    def __init__(self, seed_documents: list[DocumentChunk], embedder: EmbeddingProvider | None = None) -> None:
        self._documents: dict[str, KnowledgeDocumentUpload] = {}
        self._chunks: list[KnowledgeChunk] = []
        self.embedder = embedder or LexicalOnlyEmbedder()
        for item in seed_documents:
            self.ingest(
                KnowledgeDocumentUpload(
                    document_id=item.id,
                    title=item.title,
                    content=item.content,
                    version=item.version,
                    sku_ids=[item.sku_id],
                    tags=item.tags,
                    approved=item.approved,
                    on_sale=item.on_sale,
                    data_owner="内容审核（仿真）",
                )
            )

    def ingest(self, document: KnowledgeDocumentUpload) -> KnowledgeDocumentReceipt:
        """同 ID 上传会替换旧版本的全部切块，保证检索不混入历史版本。"""
        self._documents[document.document_id] = document
        self._chunks = [item for item in self._chunks if item.document_id != document.document_id]
        chunks = _chunk_text(document.content)
        vectors = self._embed_chunks(chunks)
        self._chunks.extend(
            KnowledgeChunk(
                id=f"{document.document_id}#chunk-{index + 1}",
                document_id=document.document_id,
                sku_ids=tuple(document.sku_ids),
                title=document.title,
                version=document.version,
                content=text,
                tags=tuple(document.tags),
                approved=document.approved,
                on_sale=document.on_sale,
                data_owner=document.data_owner,
                dense_vector=tuple(vectors[index]) if vectors else (),
            )
            for index, text in enumerate(chunks)
        )
        return KnowledgeDocumentReceipt(
            document_id=document.document_id,
            version=document.version,
            chunk_count=len(chunks),
            searchable=document.approved and document.on_sale,
            message="已入库并可检索" if document.approved and document.on_sale else "已入库，等待审核或上架",
        )

    def list_documents(self) -> list[KnowledgeDocumentUpload]:
        return list(self._documents.values())

    def status(self) -> KnowledgeBaseStatus:
        provider = self.embedder.status()
        return KnowledgeBaseStatus(
            provider=str(provider["provider"]),
            ready=bool(provider["ready"]),
            reason=str(provider["reason"]),
            document_count=len(self._documents),
            chunk_count=len(self._chunks),
            vectorized_chunk_count=sum(bool(item.dense_vector) for item in self._chunks),
        )

    def _embed_chunks(self, texts: list[str]) -> list[list[float]]:
        """向量模型不可用时显式退化为词法检索，上传与审核流程仍可继续。"""
        if isinstance(self.embedder, LexicalOnlyEmbedder):
            return []
        try:
            return self.embedder.embed(texts)
        except RuntimeError:
            return []

    def search(self, question: str, sku_ids: list[str], limit: int = 3) -> list[Evidence]:
        """先按审核/上架/SKU 过滤，再做轻量 BM25 风格排序。"""
        query_terms = _terms(question)
        allowed_skus = set(sku_ids)
        candidates = [
            item
            for item in self._chunks
            if item.approved
            and item.on_sale
            and (not allowed_skus or allowed_skus.intersection(item.sku_ids))
        ]
        if not candidates or not query_terms:
            return []
        document_frequency = {
            term: sum(term in set(_terms(item.title + item.content + " ".join(item.tags))) for item in candidates)
            for term in set(query_terms)
        }
        lexical_scores: dict[str, float] = {}
        for item in candidates:
            searchable_text = item.title + item.content + " ".join(item.tags)
            terms = _terms(searchable_text)
            score = sum(
                (terms.count(term) / max(len(terms), 1))
                * (log((len(candidates) + 1) / (document_frequency[term] + 1)) + 1)
                for term in set(query_terms)
                if term in terms
            )
            # 完整标签或标题命中比单字重合更重要，减少中文单字导致的误召回。
            score += sum(1.5 for tag in item.tags if tag and tag.lower() in question.lower())
            score += 2.0 if item.title.lower() in question.lower() else 0.0
            lexical_scores[item.id] = score

        query_vector = self._embed_chunks([question])
        semantic_scores = {
            item.id: sum(a * b for a, b in zip(query_vector[0], item.dense_vector, strict=False))
            for item in candidates
            if query_vector and item.dense_vector
        }
        max_lexical = max(lexical_scores.values(), default=0.0)
        scored: list[tuple[float, KnowledgeChunk]] = []
        for item in candidates:
            lexical = lexical_scores[item.id]
            semantic = semantic_scores.get(item.id)
            if semantic is None and lexical <= 0:
                continue
            lexical_normalized = lexical / max_lexical if max_lexical else 0.0
            score = lexical_normalized if semantic is None else 0.45 * lexical_normalized + 0.55 * max(semantic, 0.0)
            if score > 0:
                scored.append((round(score, 6), item))
        ranked = sorted(scored, key=lambda pair: pair[0], reverse=True)[:limit]
        return [
            Evidence(
                id=item.document_id,
                sku_id=item.sku_ids[0] if item.sku_ids else "",
                title=item.title,
                version=item.version,
                quote=item.content,
                retrieval_score=score,
                source_chunk_id=item.id,
            )
            for score, item in ranked
        ]
