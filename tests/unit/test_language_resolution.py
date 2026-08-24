import pytest

from social_reply.application.reply_decision.language_resolution import (
    LLM_FALLBACK_SOURCE,
    resolve_customer_language,
)


class _FakeLLM:
    """只实现语言判定能力的假 client，记录调用次数以验证不做无谓调用。"""

    def __init__(self, tag: str | None = None, raises: bool = False) -> None:
        self.tag = tag
        self.raises = raises
        self.calls: list[str] = []

    async def detect_language_tag(self, text: str) -> str | None:
        self.calls.append(text)
        if self.raises:
            raise RuntimeError("boom")
        return self.tag


class _NoCapabilityLLM:
    """模拟 StubLLMClient 之外、连方法都没有的 client。"""


@pytest.mark.asyncio
async def test_reliable_deterministic_detection_skips_llm():
    llm = _FakeLLM(tag="fr")
    result = await resolve_customer_language("こんにちは", llm=llm)
    assert result.tag == "ja"
    assert result.source == "current_message"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_llm_fallback_resolves_short_latin_greeting():
    llm = _FakeLLM(tag="es")
    result = await resolve_customer_language("Hola", llm=llm)
    assert result.tag == "es"
    assert result.source == LLM_FALLBACK_SOURCE
    assert result.is_reliable
    assert llm.calls == ["Hola"]


@pytest.mark.asyncio
async def test_llm_fallback_resolves_language_outside_deterministic_coverage():
    # 尼泊尔语被 detect_language 主动 fail-closed，兜底后应可回复。
    llm = _FakeLLM(tag="ne")
    result = await resolve_customer_language("म पैसा कसरी निकाल्न सक्छु?", llm=llm)
    assert result.tag == "ne"
    assert result.source == LLM_FALLBACK_SOURCE


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "👍", "123456", "   "])
async def test_text_without_letters_never_calls_llm(text):
    llm = _FakeLLM(tag="en")
    result = await resolve_customer_language(text, llm=llm)
    assert result.tag == "und"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_llm_returning_none_keeps_deterministic_unknown():
    llm = _FakeLLM(tag=None)
    result = await resolve_customer_language("Hola", llm=llm)
    assert result.tag == "und"
    assert result.source == "unknown"


@pytest.mark.asyncio
async def test_llm_raising_keeps_deterministic_unknown():
    llm = _FakeLLM(raises=True)
    result = await resolve_customer_language("Hola", llm=llm)
    assert result.tag == "und"


@pytest.mark.asyncio
async def test_missing_llm_or_capability_keeps_deterministic_unknown():
    assert (await resolve_customer_language("Hola", llm=None)).tag == "und"
    assert (await resolve_customer_language("Hola", llm=_NoCapabilityLLM())).tag == "und"


@pytest.mark.asyncio
async def test_llm_decides_current_message_over_history():
    # 客户从法语切到 "OK" 之外的可判定内容时，历史绝不能抢在模型之前定语言。
    llm = _FakeLLM(tag="en")
    result = await resolve_customer_language(
        "hello",
        (("user", "Comment puis-je obtenir un remboursement ?"),),
        llm=llm,
    )
    assert result.tag == "en"
    assert result.source == LLM_FALLBACK_SOURCE
    assert llm.calls == ["hello"]


@pytest.mark.asyncio
async def test_history_only_serves_messages_without_any_language():
    # "OK" 本身不含语种信息，模型也判不出，此时沿用客户此前的语言好过转人工。
    llm = _FakeLLM(tag=None)
    result = await resolve_customer_language(
        "OK",
        (("user", "Comment puis-je obtenir un remboursement ?"),),
        llm=llm,
    )
    assert result.tag == "fr"
    assert result.source == "recent_user_history"


@pytest.mark.asyncio
async def test_history_fallback_needs_no_llm_for_letterless_text():
    llm = _FakeLLM(tag="en")
    result = await resolve_customer_language("👍", (("user", "こんにちは"),), llm=llm)
    assert result.tag == "ja"
    assert result.source == "recent_user_history"
    assert llm.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["hello", "help", "Can I use WikiFX without registering?"])
async def test_english_message_is_never_answered_in_the_history_language(text):
    """生产回归：会话 4a14f1b6 的 seq 312/315/319。

    "hello" 是单个拉丁词、长句 "Can I use WikiFX..." 的 margin 只有 0.027，两者
    确定性检测都判不出；旧级联因此抄了历史里的日语，客户用英文打招呼却收到日语回复。
    """
    llm = _FakeLLM(tag="en")
    japanese_history = (
        ("user", "こんにちは"),
        ("user", "こんにちは"),
        ("user", "こんにちは"),
    )
    result = await resolve_customer_language(text, japanese_history, llm=llm)
    assert result.tag == "en"
    assert result.source == LLM_FALLBACK_SOURCE


@pytest.mark.asyncio
async def test_ambiguous_chinese_takes_script_from_history_without_calling_llm():
    # 简繁在短问候里字形相同，模型同样判不出，客户此前用过的字体才是最准的线索。
    llm = _FakeLLM(tag="zh-Hans")
    result = await resolve_customer_language(
        "退款多久", (("user", "請問退款通常需要幾天？"),), llm=llm
    )
    assert result.tag == "zh-Hant"
    assert result.source == "recent_user_history"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_ambiguous_chinese_never_degrades_into_another_language():
    llm = _FakeLLM(tag="en")
    result = await resolve_customer_language(
        "退款多久", (("user", "How do I withdraw money from my account?"),), llm=llm
    )
    assert result.tag == "zh"
    assert result.source == "current_message"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_devanagari_sibling_detection_is_confirmed_by_llm():
    # lingua 把这句印地语自信地判成马拉地语（is_reliable=True），
    # 因此不能只在 und 时才兜底，否则客户会收到错误语言的回复。
    llm = _FakeLLM(tag="hi")
    result = await resolve_customer_language("नमस्ते", llm=llm)
    assert result.tag == "hi"
    assert result.source == LLM_FALLBACK_SOURCE
    assert llm.calls == ["नमस्ते"]


@pytest.mark.asyncio
async def test_devanagari_confirmation_falls_back_to_deterministic_when_llm_unavailable():
    # 二次确认失败时保留确定性结果，绝不因此退化成 und 而白白转人工，也绝不让历史
    # 顶掉一条已经明确判出天城文的消息。
    english_history = (("user", "How do I withdraw money from my account?"),)
    result = await resolve_customer_language("नमस्ते", english_history, llm=_FakeLLM(tag=None))
    assert result.tag == "mr"
    assert result.is_reliable
    assert result.source == "current_message"

    result = await resolve_customer_language("नमस्ते", english_history, llm=_FakeLLM(raises=True))
    assert result.tag == "mr"

    result = await resolve_customer_language("नमस्ते", english_history, llm=None)
    assert result.tag == "mr"


@pytest.mark.asyncio
async def test_genuine_marathi_is_preserved_by_confirmation():
    llm = _FakeLLM(tag="mr")
    result = await resolve_customer_language("माझे पैसे कसे काढायचे आहेत?", llm=llm)
    assert result.tag == "mr"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "こんにちは",
        "안녕하세요",
        "สวัสดี",
        "Как вывести деньги со счета?",
        "كيف يمكنني سحب الأموال؟",
    ],
)
async def test_non_sibling_reliable_detection_never_pays_for_confirmation(text):
    llm = _FakeLLM(tag="en")
    result = await resolve_customer_language(text, llm=llm)
    assert result.source == "current_message"
    assert llm.calls == []
