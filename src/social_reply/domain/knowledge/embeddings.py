"""Embedding 客户端：Protocol + OpenAI 实现 + 确定性 Fake"""

import hashlib
import math
import struct
from typing import Protocol

import httpx

_DIMENSIONS = 1536


class EmbeddingResponseError(ValueError):
    """Provider returned an embedding payload that is unsafe to persist or query."""


class EmbeddingClient(Protocol):
    version: str  # 向量版本标识（入库与检索过滤必须一致，防不同来源向量混比）

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """按输入顺序返回每条文本的 embedding 向量"""
        ...


class OpenAIEmbeddingClient:
    """真实 OpenAI /v1/embeddings 客户端（httpx 可注入 transport）。

    注意：与 OpenAILLMClient.decide 的 fail-safe（任何失败降级 HANDOFF）语义不同——
    embedding 主要用于导入 CLI 与检索前置，失败应当响亮抛出让调用方决定
    （CLI 直接报错退出；决策管线侧由调用方自行捕获降级）。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        expected_dimensions: int,
        model: str = "text-embedding-3-small",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if expected_dimensions <= 0:
            raise ValueError("expected embedding dimensions must be positive")
        self._model = model
        self.expected_dimensions = expected_dimensions
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            transport=transport,
        )
        # 兼容现有数据库过滤；完整 provider identity 由后续 release manifest 承载。
        self.version = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {"model": self._model, "input": texts}
        resp = await self._client.post("/embeddings", json=payload)
        resp.raise_for_status()
        try:
            body = resp.json()
            data = body["data"]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingResponseError("embedding response is missing data") from exc
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingResponseError("embedding response count does not match input count")

        indexed: dict[int, list[float]] = {}
        for item in data:
            if not isinstance(item, dict):
                raise EmbeddingResponseError("embedding response item must be an object")
            index = item.get("index")
            embedding = item.get("embedding")
            if isinstance(index, bool) or not isinstance(index, int):
                raise EmbeddingResponseError("embedding response index must be an integer")
            if index < 0 or index >= len(texts) or index in indexed:
                raise EmbeddingResponseError("embedding response indexes are invalid")
            if not isinstance(embedding, list) or len(embedding) != self.expected_dimensions:
                raise EmbeddingResponseError(
                    f"embedding dimension mismatch: expected {self.expected_dimensions}"
                )
            vector: list[float] = []
            for value in embedding:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise EmbeddingResponseError("embedding values must be numeric")
                number = float(value)
                if not math.isfinite(number):
                    raise EmbeddingResponseError("embedding values must be finite")
                try:
                    float32 = struct.unpack("!f", struct.pack("!f", number))[0]
                except OverflowError as exc:
                    raise EmbeddingResponseError(
                        "embedding values must fit PostgreSQL vector float32"
                    ) from exc
                if not math.isfinite(float32):
                    raise EmbeddingResponseError(
                        "embedding values must fit PostgreSQL vector float32"
                    )
                vector.append(float32)
            norm = math.hypot(*vector)
            if not math.isfinite(norm) or norm <= 0.0:
                raise EmbeddingResponseError("embedding vector must have a finite non-zero norm")
            indexed[index] = vector

        expected_indexes = set(range(len(texts)))
        if set(indexed) != expected_indexes:
            raise EmbeddingResponseError("embedding response indexes are incomplete")
        return [indexed[index] for index in range(len(texts))]

    async def aclose(self) -> None:
        await self._client.aclose()


class FakeEmbeddingClient:
    """确定性伪向量：sha256 派生 1536 维并归一化，测试/无 key 环境使用"""

    version = "fake-sha256"  # 与真实模型版本隔离：伪向量绝不与 OpenAI 向量混检

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        # 以 sha256(text + 分块序号) 反复取字节，拼出 1536 维确定性向量
        raw: list[float] = []
        block = 0
        while len(raw) < _DIMENSIONS:
            digest = hashlib.sha256(f"{text}#{block}".encode()).digest()
            raw.extend(b / 255.0 for b in digest)
            block += 1
        raw = raw[:_DIMENSIONS]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]
