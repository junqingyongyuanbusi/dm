from social_reply.domain.reply.decision import ReplyAction, RiskLevel
from social_reply.domain.reply.rules import apply_rules


def test_greeting_falls_through_to_knowledge():
    # 问候语不再内置拦截（用户 CSV 模板优先）：交给知识库/LLM 处理
    assert apply_rules("你好") is None
    assert apply_rules("hello") is None


def test_risk_word_forces_handoff():
    d = apply_rules("你们是不是诈骗，我无法出金")
    assert d is not None
    assert d.action is ReplyAction.HANDOFF
    assert d.risk_level is RiskLevel.HIGH
    assert "RISK_WORD" in d.reason_codes


def test_empty_text_handoff():
    d = apply_rules("   ")
    assert d is not None and d.action is ReplyAction.HANDOFF
    assert "EMPTY_OR_NON_TEXT" in d.reason_codes
    assert apply_rules(None).action is ReplyAction.HANDOFF


def test_normal_question_falls_through_to_llm():
    assert apply_rules("请问怎么修改绑定邮箱？") is None
