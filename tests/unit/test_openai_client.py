import json

import httpx
import pytest

from social_reply.domain.reply.decision import ReplyAction, RiskLevel, Visibility
from social_reply.domain.reply.llm import LLMContext
from social_reply.domain.reply.openai_client import OpenAILLMClient

_CTX = LLMContext(text="你们几点营业？", conversation_key="cw:1:2")

_GOOD_OUTPUT = {
    "action": "auto_reply",
    "reply_text": "您好，我们每天 9:00-18:00 营业。",
    "intent": "business_hours",
    "risk_level": "low",
    "confidence": 0.9,
    "reply_visibility": "public",
}


def _completion_response(content: str, refusal: str | None = None) -> httpx.Response:
    message: dict = {"role": "assistant", "content": content}
    if refusal is not None:
        message["refusal"] = refusal
    return httpx.Response(200, json={"choices": [{"message": message}]})


def _client(handler) -> OpenAILLMClient:
    return OpenAILLMClient(
        api_key="sk-test", base_url="https://api.openai.com/v1",
        model="gpt-4o-mini", timeout=5.0,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_成功解析映射为_reply_decision():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _completion_response(json.dumps(_GOOD_OUTPUT))

    decision = await _client(handler).decide(_CTX)
    assert decision.action is ReplyAction.AUTO_REPLY
    assert decision.reply_text == _GOOD_OUTPUT["reply_text"]
    assert decision.intent == "business_hours"
    assert decision.risk_level is RiskLevel.LOW
    assert decision.confidence == 0.9
    assert decision.reply_visibility is Visibility.PUBLIC
    assert decision.reason_codes == ("OPENAI",)
    assert decision.source == "llm"
    # 请求体断言：json_schema strict + Authorization 头
    request = captured[0]
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert request.url.path.endswith("/chat/completions")
    body = json.loads(request.content)
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.asyncio
async def test_首次坏json第二次好_恰好重试一次():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _completion_response("这不是 JSON")
        return _completion_response(json.dumps(_GOOD_OUTPUT))

    decision = await _client(handler).decide(_CTX)
    assert calls["n"] == 2
    assert decision.action is ReplyAction.AUTO_REPLY


@pytest.mark.asyncio
async def test_两次坏json_降级_handoff_schema_fail():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _completion_response('{"action": "auto_reply"}')  # 缺字段

    decision = await _client(handler).decide(_CTX)
    assert calls["n"] == 2  # 恰好重试一次，不再多试
    assert decision.action is ReplyAction.HANDOFF
    assert decision.reply_text is None
    assert "LLM_SCHEMA_FAIL" in decision.reason_codes
    assert decision.source == "llm"


@pytest.mark.asyncio
async def test_超时_降级_handoff_unavailable():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("连接超时")

    decision = await _client(handler).decide(_CTX)
    assert calls["n"] == 1  # 网络错误不重试
    assert decision.action is ReplyAction.HANDOFF
    assert "LLM_UNAVAILABLE" in decision.reason_codes


@pytest.mark.asyncio
async def test_http_500_降级_handoff_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    decision = await _client(handler).decide(_CTX)
    assert decision.action is ReplyAction.HANDOFF
    assert "LLM_UNAVAILABLE" in decision.reason_codes


@pytest.mark.asyncio
async def test_refusal_降级_handoff_refusal():
    def handler(request: httpx.Request) -> httpx.Response:
        return _completion_response("", refusal="我不能协助此请求")

    decision = await _client(handler).decide(_CTX)
    assert decision.action is ReplyAction.HANDOFF
    assert "LLM_REFUSAL" in decision.reason_codes
