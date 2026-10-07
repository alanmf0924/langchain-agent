"""可选的 SiliconFlow BGE-M3 向量适配器；不可用时保留词法检索基线。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


class EmbeddingProvider(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def status(self) -> dict[str, str | bool]: ...


@dataclass
class LexicalOnlyEmbedder:
    """默认模式：不加载模型，知识库只使用确定性词法排序。"""

    name: str = "lexical"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]

    def status(self) -> dict[str, str | bool]:
        return {"provider": self.name, "ready": False, "reason": "embedding_disabled"}


class SiliconFlowBgeM3Embedder:
    """通过 SiliconFlow 的 OpenAI 兼容 Embeddings API 调用托管 BGE-M3。"""

    name = "siliconflow-bge-m3"

    def __init__(self) -> None:
        self.model_name = os.getenv("SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3")
        self.base_url = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
        self.request_timeout_seconds = self._bounded_env_int(
            "EMBEDDING_REQUEST_TIMEOUT_SECONDS", default=6, minimum=1, maximum=30
        )
        self._client = None
        self._error: str | None = None

    @staticmethod
    def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError as error:
            raise ValueError(f"{name} 必须是整数") from error
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
        return value

    def _get_client(self):
        if self._client is not None:
            return self._client
        if self._error is not None:
            raise RuntimeError(self._error)
        api_key = os.getenv("SILICONFLOW_API_KEY")
        if not api_key:
            self._error = "SILICONFLOW_API_KEY not configured"
            raise RuntimeError(self._error)
        try:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=api_key,
                base_url=self.base_url,
                timeout=self.request_timeout_seconds,
                max_retries=0,
            )
            return self._client
        except Exception as error:
            self._error = f"{type(error).__name__}: {error}"
            raise RuntimeError(self._error) from error

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self._get_client().embeddings.create(
                model=self.model_name,
                input=texts,
                encoding_format="float",
            )
            return [list(map(float, item.embedding)) for item in sorted(response.data, key=lambda item: item.index)]
        except Exception as error:
            self._error = f"{type(error).__name__}: {error}"
            raise RuntimeError(self._error) from error

    def status(self) -> dict[str, str | bool]:
        return {
            "provider": self.name,
            "model": self.model_name,
            "ready": bool(os.getenv("SILICONFLOW_API_KEY")) and self._error is None,
            "reason": self._error or "api_ready_lazy_request",
        }


def build_embedding_provider() -> EmbeddingProvider:
    """仅在显式开启时使用托管 Embeddings API，避免没有密钥时产生网络请求。"""
    mode = os.getenv("RAG_EMBEDDER", "lexical").lower()
    return (
        SiliconFlowBgeM3Embedder()
        if mode in {"siliconflow-bge-m3", "siliconflow_bge_m3"}
        else LexicalOnlyEmbedder()
    )
