from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, RiskLevel

# 高风险词默认转人工
RISK_WORDS = ("诈骗", "无法出金", "无法提现", "律师", "起诉", "退款", "账户冻结", "冻结")


def apply_rules(text: str | None) -> ReplyDecision | None:
    """确定性前置安全规则；命中即短路返回决策，否则返回 None 交给知识库/LLM。
    注：问候语不在此拦截——hello/你好 等普通消息一律走知识库模板
    （用户模板优先于任何内置话术），无命中再按 require_knowledge 降级。"""
    if text is None or not text.strip():
        return ReplyDecision(
            action=ReplyAction.HANDOFF,
            reason_codes=("EMPTY_OR_NON_TEXT",),
            source="rule",
        )
    if any(w in text for w in RISK_WORDS):
        return ReplyDecision(
            action=ReplyAction.HANDOFF,
            risk_level=RiskLevel.HIGH,
            reason_codes=("RISK_WORD",),
            source="rule",
        )
    return None
