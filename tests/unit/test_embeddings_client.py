"""Embeddings 客户端单元测试（MockTransport + Fake 确定性）"""

import json

import httpx
import pytest

from social_reply.domain.knowledge.embeddings import (
    FakeEmbeddingClient,
    OpenAIEmbeddingClient,
)


def _client(handler) -> OpenAIEmbeddingClient:
    return OpenAIEmbeddingClient(
        api_key="sk-test",
        base_url="https://api.openai.com/v1",
        model="text-embedding-3-small",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )


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

    vectors = await _client(handler).embed(["问一", "问二"])
    request = captured[0]
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert request.url.path.endswith("/embeddings")
    body = json.loads(request.content)
    assert body == {"model": "text-embedding-3-small", "input": ["问一", "问二"]}
    # data 按 index 还原为输入顺序
    assert vectors == [[0.1, 0.1], [0.2, 0.2]]


@pytest.mark.asyncio
async def test_http_错误直接抛出():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).embed(["问一"])


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
