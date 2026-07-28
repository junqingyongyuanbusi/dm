from dataclasses import dataclass, replace

from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.domain.reply.guard import redact_pii, run_final_guard
from social_reply.domain.reply.llm import LLMClient, LLMContext
from social_reply.domain.reply.rules import apply_rules

# 低置信度的 LLM handoff 不应让会话静默，也不应永久锁死等待人工。
LLM_HANDOFF_FALLBACK_TEXT = "抱歉，我暂时无法准确回答这个问题。请换一种说法或提供更多信息。"


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


async def run_decision_pipeline(
    snapshot: DecisionSnapshot,
    *,
    llm: LLMClient,
    killswitch,
    knowledge: tuple[str, ...] = (),
    require_knowledge: bool = False,
    verbatim_reply: str | None = None,
    history: tuple[tuple[str, str], ...] = (),
    persona: str | None = None,
) -> ReplyDecision:
    """纯管线：状态门 → kill switch → 安全规则 → 模板直答/LLM → Final Guard → 草稿降级。
    不触碰数据库、不持有事务（真实 LLM 慢调用不阻塞入站与接管翻转）。
    verbatim_reply 非空时（知识库命中且开模板直答）原文返回模板回复，不调 LLM。"""
    # Human takeover suppresses all automated decisions.
    if snapshot.automation_state == "HUMAN_ACTIVE":
        return ReplyDecision(
            action=ReplyAction.IGNORE, reason_codes=("HUMAN_ACTIVE",), source="rule"
        )

    # 全局/品牌/账号急停：降级为草稿（仍生成供人工参考，但不外发）。
    # kill switch 是安全控制：无法验证急停状态时 fail-closed。
    try:
        disabled = await killswitch.is_disabled(
            snapshot.brand_id, snapshot.account_id, snapshot.tenant_id
        )
    except Exception:
        return ReplyDecision(
            action=ReplyAction.DRAFT, reason_codes=("KILLSWITCH_UNAVAILABLE",), source="rule"
        )
    if disabled:
        return ReplyDecision(action=ReplyAction.DRAFT, reason_codes=("KILLSWITCH",), source="rule")

    # 确定性安全规则（空消息/风险词）优先于一切
    ruled = apply_rules(snapshot.text)
    if ruled is not None:
        decision = ruled
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
        decision = await llm.decide(
            LLMContext(
                text=redact_pii(snapshot.text or ""),
                conversation_key=snapshot.conversation_key,
                knowledge=knowledge,
                history=safe_history,
                persona=persona,
            )
        )
        if knowledge:
            # Preserve whether retrieved knowledge influenced the model decision.
            decision = replace(decision, reason_codes=decision.reason_codes + ("KNOWLEDGE_HIT",))

    # 仅把 LLM 自身给出的 handoff 转为公开兜底；风险词与知识不足是确定性规则（source=rule），
    # 必须保持 handoff。Guard 失败发生在下一步，其 source=guard，因此不会被这里回滚。
    if decision.action is ReplyAction.HANDOFF and decision.source == "llm":
        decision = ReplyDecision(
            action=ReplyAction.AUTO_REPLY,
            reply_text=LLM_HANDOFF_FALLBACK_TEXT,
            reason_codes=decision.reason_codes + ("LLM_HANDOFF_FALLBACK",),
            source="rule",
        )

    # 输出侧闸门
    decision = run_final_guard(decision, snapshot.platform)

    # 草稿降级必须是管线的最后一步：任何把 action 改回 AUTO_REPLY 的兜底都要排在它前面，
    # 否则决策会以 auto_reply 落库——BOT_DRAFT_ONLY 下既不外发，也进不了 admin 待审队列。
    # Persistence creates a public Outbox only when state and version still match BOT_ACTIVE.
    # Keep this draft downgrade separate from that send-time race check.
    if snapshot.automation_state == "BOT_DRAFT_ONLY" and decision.action is ReplyAction.AUTO_REPLY:
        decision = replace(decision, action=ReplyAction.DRAFT)

    return decision
