from dataclasses import replace

from social_reply.domain.reply.decision import (
    ReplyAction,
    ReplyDecision,
    RiskLevel,
    Visibility,
)


def test_defaults_and_replace():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="hi")
    assert d.risk_level is RiskLevel.LOW
    assert d.reply_visibility is Visibility.PUBLIC
    assert d.reason_codes == ()
    d2 = replace(d, action=ReplyAction.DRAFT)
    assert d2.action is ReplyAction.DRAFT and d2.reply_text == "hi"


def test_str_enums_are_strings():
    assert ReplyAction.HANDOFF == "handoff"
    assert RiskLevel.HIGH == "high"
    assert Visibility.PRIVATE == "private"
