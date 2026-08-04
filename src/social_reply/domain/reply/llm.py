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
    # 同会话历史消息（按时间升序），元素为 (role, text)：
    # role ∈ {"user", "assistant"}，不含当前这条。默认空 → 单轮行为不变。
    history: tuple[tuple[str, str], ...] = ()
    # 租户在后台编辑的品牌语气、风格与本地化偏好。None → 使用代码内置默认偏好。
    # WikiFX 身份、领域事实边界、动作语义与安全规则由 CONTRACT_PROMPT 固定追加。
    persona: str | None = None


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
