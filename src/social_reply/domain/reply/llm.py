from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from social_reply.domain.reply.business_prompt import BusinessPromptInstructions
from social_reply.domain.reply.decision import (
    ReplyAction,
    ReplyDecision,
    RiskLevel,
    Visibility,
)
from social_reply.domain.reply.voice import VoicePreferences

APPROVED_VERBATIM_SENTINEL = "__APPROVED_VERBATIM__"


@dataclass(frozen=True)
class RAGCandidate:
    candidate_id: str
    question: str
    approved_answer: str
    similarity: float


@dataclass(frozen=True)
class RAGSelectionResult:
    selected_candidate_id: str | None
    directly_answers: bool = False
    requires_case_specific_data: bool = False
    has_conflict: bool = False


@dataclass(frozen=True)
class RAGVerificationResult:
    relevant: bool
    faithful: bool


class KnowledgeAmbiguityOutcome(StrEnum):
    ANSWER = "answer"
    CLARIFY = "clarify"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class KnowledgeAmbiguityResolution:
    outcome: KnowledgeAmbiguityOutcome
    reply_text: str
    used_candidate_ids: tuple[str, ...]


def normalize_knowledge_ambiguity_resolution(
    resolution: KnowledgeAmbiguityResolution,
    *,
    allowed_candidate_ids: frozenset[str],
) -> KnowledgeAmbiguityResolution | None:
    if not isinstance(resolution, KnowledgeAmbiguityResolution):
        return None
    if not isinstance(resolution.outcome, KnowledgeAmbiguityOutcome):
        return None
    if not isinstance(resolution.reply_text, str):
        return None
    if not isinstance(resolution.used_candidate_ids, (list, tuple)) or any(
        not isinstance(candidate_id, str)
        for candidate_id in resolution.used_candidate_ids
    ):
        return None
    reply_text = resolution.reply_text.strip()
    used_candidate_ids = tuple(resolution.used_candidate_ids)
    used_candidate_id_set = set(used_candidate_ids)
    if len(used_candidate_id_set) != len(used_candidate_ids):
        return None
    if not used_candidate_id_set <= allowed_candidate_ids:
        return None
    if resolution.outcome is KnowledgeAmbiguityOutcome.ABSTAIN:
        if reply_text or used_candidate_ids:
            return None
    elif not reply_text or not used_candidate_ids:
        return None
    if (
        resolution.outcome is KnowledgeAmbiguityOutcome.CLARIFY
        and used_candidate_id_set != allowed_candidate_ids
    ):
        return None
    return KnowledgeAmbiguityResolution(
        outcome=resolution.outcome,
        reply_text=reply_text,
        used_candidate_ids=used_candidate_ids,
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
    voice_preferences: VoicePreferences | None = None
    business_prompt: BusinessPromptInstructions | None = None
    target_language: str = "und"
    approved_verbatim_available: bool = False

    def __post_init__(self) -> None:
        if self.voice_preferences is not None and not isinstance(
            self.voice_preferences, VoicePreferences
        ):
            raise TypeError("voice_preferences_must_be_typed")
        if self.business_prompt is not None and not isinstance(
            self.business_prompt, BusinessPromptInstructions
        ):
            raise TypeError("business_prompt_must_be_typed")


class LLMClient(Protocol):
    knowledge_ambiguity_resolver_id: str

    async def decide(self, context: LLMContext) -> ReplyDecision: ...

    async def generate_knowledge_reply_text(self, context: LLMContext) -> str | None:
        """Generate customer-facing text without choosing a reply action."""
        ...

    async def resolve_knowledge_ambiguity(
        self,
        context: LLMContext,
        *,
        candidates: tuple[RAGCandidate, ...],
    ) -> KnowledgeAmbiguityResolution:
        """Resolve two sufficiently similar but competing approved answers."""
        ...

    async def verify_grounding(
        self,
        *,
        approved_reply: str,
        candidate_reply: str,
        target_language: str,
    ) -> bool: ...

    async def select_rag_answer(
        self,
        *,
        query: str,
        candidates: tuple[RAGCandidate, ...],
    ) -> RAGSelectionResult: ...

    async def verify_rag_answer(
        self,
        *,
        query: str,
        approved_reply: str,
        candidate_reply: str,
        target_language: str,
    ) -> RAGVerificationResult: ...

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
    knowledge_ambiguity_resolver_id = "knowledge-ambiguity-resolver-v1:stub"
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

    async def generate_knowledge_reply_text(self, context: LLMContext) -> str | None:
        return "您好，已收到您的问题，我们会尽快为您解答。"

    async def resolve_knowledge_ambiguity(
        self,
        context: LLMContext,
        *,
        candidates: tuple[RAGCandidate, ...],
    ) -> KnowledgeAmbiguityResolution:
        return KnowledgeAmbiguityResolution(
            outcome=KnowledgeAmbiguityOutcome.ABSTAIN,
            reply_text="",
            used_candidate_ids=(),
        )

    async def verify_grounding(
        self,
        *,
        approved_reply: str,
        candidate_reply: str,
        target_language: str,
    ) -> bool:
        return True

    async def select_rag_answer(
        self,
        *,
        query: str,
        candidates: tuple[RAGCandidate, ...],
    ) -> RAGSelectionResult:
        # The stub cannot translate or judge relevance. Abstaining keeps tests and local smoke safe.
        return RAGSelectionResult(selected_candidate_id=None)

    async def verify_rag_answer(
        self,
        *,
        query: str,
        approved_reply: str,
        candidate_reply: str,
        target_language: str,
    ) -> RAGVerificationResult:
        return RAGVerificationResult(relevant=True, faithful=True)

    async def translate_to_english(self, text: str) -> str | None:
        # Stub 不提供翻译：回退路径静默关闭，不影响主路径。
        return None

    async def detect_language_tag(self, text: str) -> str | None:
        # Stub 不提供语言判定：兜底路径静默关闭，确定性检测结果原样生效。
        return None
