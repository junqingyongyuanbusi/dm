"""Embeddings 客户端单元测试（MockTransport + Fake 确定性）"""

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from social_reply.domain.knowledge.embeddings import (
    EmbeddingResponseError,
    FakeEmbeddingClient,
    OpenAIEmbeddingClient,
)


@asynccontextmanager
async def _client(handler, *, expected_dimensions: int = 2):
    client = OpenAIEmbeddingClient(
        api_key="sk-test",
        base_url="https://api.openai.com/v1",
        expected_dimensions=expected_dimensions,
        model="text-embedding-3-small",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_请求体与顺序映射():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        # 故意乱序返回，验证按 index 排序还原
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.2, 0.2]},
                    {"index": 0, "embedding": [0.1, 0.1]},
                ],
            },
        )

    async with _client(handler) as client:
        vectors = await client.embed(["问一", "问二"])
    request = captured[0]
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert request.url.path.endswith("/embeddings")
    body = json.loads(request.content)
    assert body == {"model": "text-embedding-3-small", "input": ["问一", "问二"]}
    # data 按 index 还原为输入顺序
    assert vectors[0] == pytest.approx([0.1, 0.1])
    assert vectors[1] == pytest.approx([0.2, 0.2])


@pytest.mark.asyncio
async def test_http_错误直接抛出():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    async with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.embed(["问一"])


@pytest.mark.asyncio
async def test_fake_确定性与维度():
    fake = FakeEmbeddingClient()
    [v1] = await fake.embed(["退款多久到账"])
    [v2, v3] = await fake.embed(["退款多久到账", "怎么改邮箱"])
    assert len(v1) == 1536
    assert v1 == v2  # 同文本同向量
    assert v1 != v3  # 不同文本不同向量
    # 已归一化：模长约为 1
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-6


@pytest.mark.asyncio
async def test_empty_input_does_not_call_provider():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("provider must not be called for empty input")

    async with _client(handler) as client:
        assert await client.embed([]) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": []},
        {"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
        {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 0, "embedding": [0.3, 0.4]},
            ]
        },
        {
            "data": [
                {"index": 0, "embedding": [0.1]},
                {"index": 1, "embedding": [0.3, 0.4]},
            ]
        },
        {
            "data": [
                {"index": 0, "embedding": [float("nan"), 0.2]},
                {"index": 1, "embedding": [0.3, 0.4]},
            ]
        },
        {
            "data": [
                {"index": 0, "embedding": ["bad", 0.2]},
                {"index": 1, "embedding": [0.3, 0.4]},
            ]
        },
        {
            "data": [
                {"index": 0, "embedding": [0.0, 0.0]},
                {"index": 1, "embedding": [0.3, 0.4]},
            ]
        },
        {
            "data": [
                {"index": 0, "embedding": [1e39, 0.2]},
                {"index": 1, "embedding": [0.3, 0.4]},
            ]
        },
    ],
)
@pytest.mark.asyncio
async def test_malformed_embedding_response_is_rejected(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(payload, allow_nan=True).encode(),
            headers={"Content-Type": "application/json"},
        )

    async with _client(handler) as client:
        with pytest.raises(EmbeddingResponseError):
            await client.embed(["问一", "问二"])


@pytest.mark.asyncio
async def test_default_client_rejects_non_1536_dimension_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    async with _client(handler, expected_dimensions=1536) as client:
        with pytest.raises(EmbeddingResponseError, match="dimension mismatch"):
            await client.embed(["问一"])
