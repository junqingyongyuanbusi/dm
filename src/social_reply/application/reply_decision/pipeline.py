from dataclasses import dataclass, replace

from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.domain.reply.guard import run_final_guard
from social_reply.domain.reply.llm import LLMClient, LLMContext
from social_reply.domain.reply.rules import apply_rules


@dataclass(frozen=True)
class DecisionSnapshot:
    """决策入口读到的会话快照——纯数据，脱离数据库会话。
    state_version 用于 tx2 的 CAS（防接管竞态 defense 1）。"""
    text: str | None
    platform: str
    brand_id: str
    account_id: str
    conversation_key: str
    automation_state: str
    state_version: int


async def run_decision_pipeline(
    snapshot: DecisionSnapshot, *, llm: LLMClient, killswitch
) -> ReplyDecision:
    """纯管线：状态门 → kill switch → 规则 → LLM → Final Guard → 草稿降级。
    不触碰数据库、不持有事务（真实 LLM 慢调用不阻塞入站与接管翻转）。"""
    # 状态门：人工接管中，AI 一律不自动发（PLAN.md §六）
    if snapshot.automation_state == "HUMAN_ACTIVE":
        return ReplyDecision(action=ReplyAction.IGNORE,
                             reason_codes=("HUMAN_ACTIVE",), source="rule")

    # 全局/品牌/账号急停：降级为草稿（仍生成供人工参考，但不外发）。
    # kill switch 是安全控制：无法验证急停状态时 fail-closed（Task 5 评审）。
    try:
        disabled = await killswitch.is_disabled(snapshot.brand_id, snapshot.account_id)
    except Exception:
        return ReplyDecision(action=ReplyAction.DRAFT,
                             reason_codes=("KILLSWITCH_UNAVAILABLE",), source="rule")
    if disabled:
        return ReplyDecision(action=ReplyAction.DRAFT,
                             reason_codes=("KILLSWITCH",), source="rule")

    # 确定性规则优先于 LLM
    ruled = apply_rules(snapshot.text)
    if ruled is not None:
        decision = ruled
    else:
        decision = await llm.decide(
            LLMContext(text=snapshot.text or "", conversation_key=snapshot.conversation_key)
        )

    # 输出侧闸门
    decision = run_final_guard(decision, snapshot.platform)

    # 草稿先行：BOT_DRAFT_ONLY 把 auto_reply 降级为 draft（PLAN.md §十八）
    if snapshot.automation_state == "BOT_DRAFT_ONLY" and decision.action is ReplyAction.AUTO_REPLY:
        decision = replace(decision, action=ReplyAction.DRAFT)

    return decision
