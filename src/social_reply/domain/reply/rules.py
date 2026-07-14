from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, RiskLevel

# PLAN.md §五：高风险词默认转人工
RISK_WORDS = ("诈骗", "无法出金", "无法提现", "律师", "起诉", "退款", "账户冻结", "冻结")
GREETINGS = frozenset({"hi", "hello", "hey", "你好", "您好", "thanks", "thank you", "谢谢"})
GREETING_REPLY = "您好，请问有什么可以帮您？"


def apply_rules(text: str | None) -> ReplyDecision | None:
    """确定性前置规则；命中即短路返回决策，否则返回 None 交给 LLM。"""
    if text is None or not text.strip():
        return ReplyDecision(
            action=ReplyAction.HANDOFF,
            reason_codes=("EMPTY_OR_NON_TEXT",),
            source="rule",
        )
    if text.strip().lower() in GREETINGS:
        return ReplyDecision(
            action=ReplyAction.AUTO_REPLY,
            reply_text=GREETING_REPLY,
            intent="greeting",
            reason_codes=("GREETING_TEMPLATE",),
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
