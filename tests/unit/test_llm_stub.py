from social_reply.domain.reply.decision import ReplyAction
from social_reply.domain.reply.llm import LLMContext, StubLLMClient


async def test_stub_returns_deterministic_auto_reply():
    client = StubLLMClient()
    d = await client.decide(LLMContext(text="怎么改邮箱", conversation_key="telegram:acc:9"))
    assert d.action is ReplyAction.AUTO_REPLY
    assert d.reply_text
    assert d.source == "llm"
    assert "STUB_LLM" in d.reason_codes


async def test_stub_is_pure_same_input_same_output():
    client = StubLLMClient()
    ctx = LLMContext(text="x", conversation_key="k")
    assert await client.decide(ctx) == await client.decide(ctx)
