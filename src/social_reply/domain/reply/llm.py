from dataclasses import dataclass
from typing import Protocol

from social_reply.domain.reply.decision import (
    ReplyAction,
    ReplyDecision,
    RiskLevel,
    Visibility,
)


@dataclass(frozen=True)
class LLMContext:
    text: str
    conversation_key: str
    # 检索命中的官方回复模板文本（默认空，向后兼容）
    knowledge: tuple[str, ...] = ()


class LLMClient(Protocol):
    async def decide(self, context: LLMContext) -> ReplyDecision: ...


class StubLLMClient:
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
