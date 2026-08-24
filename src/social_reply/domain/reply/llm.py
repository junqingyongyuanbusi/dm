from dataclasses import dataclass
from typing import Protocol

from social_reply.domain.reply.decision import (
    ReplyAction,
    ReplyDecision,
    RiskLevel,
    Visibility,
)
from social_reply.domain.reply.voice import VoicePreferences

APPROVED_VERBATIM_SENTINEL = "__APPROVED_VERBATIM__"


@dataclass(frozen=True)
class LLMContext:
    text: str
    conversation_key: str
    # 检索命中的官方回复模板文本（默认空，向后兼容）
    knowledge: tuple[str, ...] = ()
    # 同会话历史消息（按时间升序），元素为 (role, text)：
    # role ∈ {"user", "assistant"}，不含当前这条。默认空 → 单轮行为不变。
    history: tuple[tuple[str, str], ...] = ()
    voice_preferences: VoicePreferences | None = None
    target_language: str = "und"
    approved_verbatim_available: bool = False

    def __post_init__(self) -> None:
        if self.voice_preferences is not None and not isinstance(
            self.voice_preferences, VoicePreferences
        ):
            raise TypeError("voice_preferences_must_be_typed")


class LLMClient(Protocol):
    async def decide(self, context: LLMContext) -> ReplyDecision: ...

    async def verify_grounding(
        self,
        *,
        approved_reply: str,
        candidate_reply: str,
        target_language: str,
    ) -> bool: ...

    async def translate_to_english(self, text: str) -> str | None:
        """查询翻译回退用：把客户查询译成英语。不可用/失败返回 None（fail-closed）。"""
        ...

    async def detect_language_tag(self, text: str) -> str | None:
        """语言兜底判定：确定性检测判不出语种时使用，返回 BCP-47 标签。

        与 translate_to_english 同约定——能力不可用、调用失败、或返回值不是合法
        BCP-47 标签时返回 None，调用方按"检测不出"继续（fail-closed）。
        """
        ...


class StubLLMClient:
    grounding_verifier_id = "grounding-v1:stub"
    """确定性桩：真实供应商接入前用于跑通管线（先 Stub 后接真）。
    不做任何网络调用，输出与输入无关的固定 auto_reply，便于端到端验证。"""

    async def decide(self, context: LLMContext) -> ReplyDecision:
        return ReplyDecision(
            action=ReplyAction.AUTO_REPLY,
            reply_text="您好，已收到您的问题，我们会尽快为您解答。",
            intent="general_question",
            risk_level=RiskLevel.LOW,
            confidence=0.6,
            reply_visibility=Visibility.PUBLIC,
            reason_codes=("STUB_LLM",),
            source="llm",
        )

    async def verify_grounding(
        self,
        *,
        approved_reply: str,
        candidate_reply: str,
        target_language: str,
    ) -> bool:
        return True

    async def translate_to_english(self, text: str) -> str | None:
        # Stub 不提供翻译：回退路径静默关闭，不影响主路径。
        return None

    async def detect_language_tag(self, text: str) -> str | None:
        # Stub 不提供语言判定：兜底路径静默关闭，确定性检测结果原样生效。
        return None
