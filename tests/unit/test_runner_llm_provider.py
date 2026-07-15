import pytest
from pydantic import ValidationError

from social_reply.application.reply_decision import runner
from social_reply.domain.reply.llm import StubLLMClient
from social_reply.domain.reply.openai_client import OpenAILLMClient
from social_reply.shared.config import get_settings


@pytest.fixture
def reset_llm_and_settings(monkeypatch):
    """隔离 settings lru_cache 与 runner._llm 单例，测试结束恢复，避免污染其他测试。"""
    monkeypatch.setenv("TESTING", "true")
    try:
        get_settings.cache_clear()
        runner._llm = None
        yield monkeypatch
    finally:
        get_settings.cache_clear()
        runner._llm = None


def test_默认_stub_provider(reset_llm_and_settings):
    reset_llm_and_settings.setenv("LLM_PROVIDER", "stub")
    llm = runner._get_llm()
    assert isinstance(llm, StubLLMClient)
    assert runner._get_llm() is llm  # 单例


def test_openai_provider(reset_llm_and_settings):
    reset_llm_and_settings.setenv("LLM_PROVIDER", "openai")
    reset_llm_and_settings.setenv("OPENAI_API_KEY", "sk-test")
    llm = runner._get_llm()
    assert isinstance(llm, OpenAILLMClient)


def test_未知_provider_在配置层被拒(reset_llm_and_settings):
    # llm_provider 已收紧为 Literal["stub","openai"]（2c 终审 I1）：
    # 配错在 Settings 构造期即报错，而非每条消息 _get_llm 抛错致决策丢失
    reset_llm_and_settings.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(ValidationError):
        get_settings()
