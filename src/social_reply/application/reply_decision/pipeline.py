import logging
import time
from dataclasses import dataclass, replace

from social_reply.domain.messages.canonical import ChannelType
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, Visibility
from social_reply.domain.reply.guard import redact_pii, run_final_guard
from social_reply.domain.reply.llm import APPROVED_VERBATIM_SENTINEL, LLMClient, LLMContext
from social_reply.domain.reply.rules import apply_rules
from social_reply.domain.reply.voice import VoicePreferences

logger = logging.getLogger(__name__)
_GROUNDING_VERIFIER_VERSION = "grounding-v1"


@dataclass(frozen=True)
class DecisionSnapshot:
    """决策入口读到的会话快照——纯数据，脱离数据库会话。
    state_version 用于 tx2 的 CAS（防接管竞态 defense 1）。"""

    text: str | None
    platform: str
    tenant_id: str
    brand_id: str
    account_id: str
    conversation_key: str
    automation_state: str
    state_version: int
    channel_type: ChannelType = ChannelType.DM
    has_unsupported_attachment: bool = False


async def run_decision_pipeline(
    snapshot: DecisionSnapshot,
    *,
    llm: LLMClient | None,
    killswitch,
    knowledge: tuple[str, ...] = (),
    require_knowledge: bool = False,
    verbatim_reply: str | None = None,
    approved_official_contact_reply: str | None = None,
    approved_knowledge_reply: str | None = None,
    verbatim_after_decision: str | None = None,
    forced_decision: ReplyDecision | None = None,
    target_language: str = "und",
    apply_legacy_rules: bool = True,
    history: tuple[tuple[str, str], ...] = (),
    voice_preferences: VoicePreferences | None = None,
    email_auto_reply_allowed: bool = True,
) -> ReplyDecision:
    """纯管线：状态门 → kill switch → 安全规则 → 模板直答/LLM → Final Guard → 草稿降级。
    不触碰数据库、不持有事务（真实 LLM 慢调用不阻塞入站与接管翻转）。
    verbatim_reply 非空时（知识库命中且开模板直答）原文返回模板回复，不调 LLM。"""
    # Only active automation modes are allowed to spend model capacity or create decisions.
    if snapshot.automation_state not in {"BOT_ACTIVE", "BOT_DRAFT_ONLY"}:
        return ReplyDecision(
            action=ReplyAction.IGNORE,
            reason_codes=(snapshot.automation_state,),
            source="rule",
        )

    # 全局/品牌/账号急停：降级为草稿（仍生成供人工参考，但不外发）。
    # kill switch 是安全控制：无法验证急停状态时 fail-closed。
    try:
        disabled = await killswitch.is_disabled(
            snapshot.brand_id, snapshot.account_id, snapshot.tenant_id
        )
    except Exception:
        logger.exception(
            "kill switch lookup failed; decision downgraded to draft",
            extra={
                "tenant_id": snapshot.tenant_id,
                "brand_id": snapshot.brand_id,
                "account_id": snapshot.account_id,
            },
        )
        return ReplyDecision(
            action=ReplyAction.DRAFT, reason_codes=("KILLSWITCH_UNAVAILABLE",), source="rule"
        )
    if disabled:
        return ReplyDecision(action=ReplyAction.DRAFT, reason_codes=("KILLSWITCH",), source="rule")

    # 确定性安全规则（空消息/风险词）优先于一切
    ruled = apply_rules(snapshot.text) if apply_legacy_rules else None
    if snapshot.has_unsupported_attachment:
        decision = ReplyDecision(
            action=ReplyAction.HANDOFF,
            reason_codes=("UNSUPPORTED_ATTACHMENT",),
            source="rule",
        )
    elif ruled is not None:
        decision = ruled
    elif forced_decision is not None:
        decision = forced_decision
    elif verbatim_reply is not None:
        # Exact templates bypass the LLM so approved wording is preserved.
        decision = ReplyDecision(
            action=ReplyAction.AUTO_REPLY,
            reply_text=verbatim_reply,
            intent="knowledge_template",
            confidence=1.0,
            reason_codes=("KNOWLEDGE_VERBATIM",),
            source="knowledge",
        )
    elif require_knowledge and not knowledge:
        # Knowledge-required deployments hand off rather than answer without evidence.
        decision = ReplyDecision(
            action=ReplyAction.HANDOFF, reason_codes=("INSUFFICIENT_KNOWLEDGE",), source="rule"
        )
    else:
        safe_history = tuple(
            (role, redact_pii(text))
            for role, text in history
            if role in {"user", "assistant"} and text
        )
        if llm is None:
            decision = ReplyDecision(
                action=ReplyAction.HANDOFF,
                reason_codes=("LLM_UNAVAILABLE",),
                source="rule",
            )
        else:
            decision = await llm.decide(
                LLMContext(
                    text=redact_pii(snapshot.text or ""),
                    conversation_key=snapshot.conversation_key,
                    knowledge=knowledge,
                    history=safe_history,
                    voice_preferences=voice_preferences,
                    target_language=target_language,
                    approved_verbatim_available=verbatim_after_decision is not None,
                )
            )
        if knowledge:
            # Preserve whether retrieved knowledge influenced the model decision.
            decision = replace(decision, reason_codes=decision.reason_codes + ("KNOWLEDGE_HIT",))

    if verbatim_after_decision is not None and decision.action is ReplyAction.AUTO_REPLY:
        if decision.reply_text != APPROVED_VERBATIM_SENTINEL:
            decision = replace(
                decision,
                action=ReplyAction.HANDOFF,
                reply_text=None,
                source="guard",
                reason_codes=decision.reason_codes + ("VERBATIM_SENTINEL_MISSING",),
            )
        else:
            decision = replace(
                decision,
                reply_text=verbatim_after_decision,
                source="knowledge",
                reason_codes=decision.reason_codes + ("KNOWLEDGE_VERBATIM",),
            )
    # Customer sends are public at this boundary; delivery channels own effective visibility.
    if (
        decision.action is ReplyAction.AUTO_REPLY
        and decision.reply_visibility is not Visibility.PUBLIC
    ):
        if snapshot.platform in {"facebook", "instagram"} and (
            snapshot.channel_type is ChannelType.COMMENT
        ):
            reason = (
                "FACEBOOK_COMMENT_PUBLIC"
                if snapshot.platform == "facebook"
                else "INSTAGRAM_COMMENT_PUBLIC"
            )
        else:
            reason = "AUTO_REPLY_VISIBILITY_PUBLIC"
        decision = replace(
            decision,
            reply_visibility=Visibility.PUBLIC,
            reason_codes=decision.reason_codes + (reason,),
        )

    # 输出侧闸门
    decision = run_final_guard(
        decision,
        snapshot.platform,
        approved_official_contact_reply=approved_official_contact_reply,
        expected_reply_language=target_language,
        approved_knowledge_reply=approved_knowledge_reply,
    )

    if (
        target_language != "und"
        and approved_knowledge_reply is not None
        and verbatim_after_decision is None
        and decision.action is ReplyAction.AUTO_REPLY
    ):
        faithful = False
        verifier = getattr(llm, "verify_grounding", None) if llm is not None else None
        verification_started = time.perf_counter()
        if verifier is not None:
            try:
                faithful = await verifier(
                    approved_reply=approved_knowledge_reply,
                    candidate_reply=decision.reply_text or "",
                    target_language=target_language,
                )
            except Exception:
                logger.exception("grounding verifier failed; decision downgraded to handoff")
        decision = replace(
            decision,
            grounding_verified=faithful,
            grounding_verifier_version=getattr(
                llm,
                "grounding_verifier_id",
                _GROUNDING_VERIFIER_VERSION,
            ),
            grounding_latency_ms=(time.perf_counter() - verification_started) * 1000,
        )
        if not faithful:
            decision = replace(
                decision,
                action=ReplyAction.HANDOFF,
                reply_text=None,
                source="guard",
                reason_codes=decision.reason_codes + ("GUARD_KNOWLEDGE_SEMANTIC_MISMATCH",),
            )
    # 草稿降级必须是管线的最后一步：任何把 action 改回 AUTO_REPLY 的兜底都要排在它前面，
    # 否则决策会以 auto_reply 落库——BOT_DRAFT_ONLY 下既不外发，也进不了 admin 待审队列。
    # Persistence creates a public Outbox only when state and version still match BOT_ACTIVE.
    # Keep this draft downgrade separate from that send-time race check.
    if snapshot.automation_state == "BOT_DRAFT_ONLY" and decision.action is ReplyAction.AUTO_REPLY:
        decision = replace(
            decision,
            action=ReplyAction.DRAFT,
            reply_visibility=Visibility.PRIVATE,
        )
    elif (
        snapshot.platform == "email"
        and not email_auto_reply_allowed
        and decision.action is ReplyAction.AUTO_REPLY
    ):
        decision = replace(
            decision,
            action=ReplyAction.DRAFT,
            reply_visibility=Visibility.PRIVATE,
            reason_codes=decision.reason_codes + ("EMAIL_AUTO_REPLY_DISABLED",),
        )

    return decision
