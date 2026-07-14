from social_reply.domain.reply.decision import (
    ReplyAction,
    ReplyDecision,
    Visibility,
)
from social_reply.domain.reply.guard import run_final_guard


def test_non_auto_reply_passes_through_untouched():
    d = ReplyDecision(action=ReplyAction.HANDOFF, reason_codes=("RISK_WORD",))
    assert run_final_guard(d, "telegram") is d


def test_public_reply_with_pii_downgraded_to_handoff():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY,
                      reply_text="您的账户 88123456 已处理",
                      reply_visibility=Visibility.PUBLIC)
    out = run_final_guard(d, "telegram")
    assert out.action is ReplyAction.HANDOFF
    assert "GUARD_PII_LEAK" in out.reason_codes


def test_email_in_public_reply_blocked():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY,
                      reply_text="请联系 a@b.com", reply_visibility=Visibility.PUBLIC)
    assert run_final_guard(d, "telegram").action is ReplyAction.HANDOFF


def test_too_long_downgraded():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="x" * 5000)
    out = run_final_guard(d, "telegram")
    assert out.action is ReplyAction.HANDOFF
    assert "GUARD_TOO_LONG" in out.reason_codes


def test_empty_reply_blocked():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="  ")
    assert run_final_guard(d, "telegram").action is ReplyAction.HANDOFF


def test_clean_reply_passes():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="您好，请提供订单号。")
    assert run_final_guard(d, "telegram").action is ReplyAction.AUTO_REPLY
