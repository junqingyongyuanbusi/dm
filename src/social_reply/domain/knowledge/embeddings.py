"""Embedding 客户端：Protocol + OpenAI 实现 + 确定性 Fake"""

import hashlib
import math
from typing import Protocol

import httpx

_DIMENSIONS = 1536


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
        model: str = "text-embedding-3-small",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            transport=transport,
        )
        self.version = model  # 版本即模型名：换模型后旧向量按版本过滤不可比

    async def embed(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self._model, "input": texts}
        resp = await self._client.post("/embeddings", json=payload)
        resp.raise_for_status()
        data = resp.json()["data"]
        # OpenAI 文档保证 data 含 index；按 index 排序防乱序
        ordered = sorted(data, key=lambda item: item["index"])
        return [item["embedding"] for item in ordered]

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
