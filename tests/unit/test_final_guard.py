import pytest

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
    d = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text="您的账户 88123456 已处理",
        reply_visibility=Visibility.PUBLIC,
    )
    out = run_final_guard(d, "telegram")
    assert out.action is ReplyAction.HANDOFF
    assert out.reply_text is None
    assert "GUARD_PII_LEAK" in out.reason_codes


@pytest.mark.parametrize(
    "template",
    (
        "Official support: support@example.com",
        "Official site: https://support.example.com/help",
        "Official account: @WikiFXSupport",
        "Telegram ID: wikifx_support",
        "Customer service hotline: 12345",
    ),
)
def test_approved_official_contact_template_passes_only_verbatim_from_knowledge(template):
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text=template,
        source="knowledge",
    )
    assert (
        run_final_guard(
            decision,
            "telegram",
            approved_official_contact_reply=template,
        )
        is decision
    )


@pytest.mark.parametrize(
    "modified_text",
    (
        "Please use Official support: support@example.com",
        " Official support: support@example.com",
        "Official support: support@example.com ",
    ),
)
def test_approved_contact_exemption_rejects_llm_copy_and_modified_text(modified_text):
    template = "Official support: support@example.com"
    llm_copy = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text=template,
        source="llm",
    )
    modified = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text=modified_text,
        source="knowledge",
    )
    assert (
        run_final_guard(
            llm_copy,
            "telegram",
            approved_official_contact_reply=template,
        ).action
        is ReplyAction.HANDOFF
    )
    assert (
        run_final_guard(
            modified,
            "telegram",
            approved_official_contact_reply=template,
        ).action
        is ReplyAction.HANDOFF
    )


def test_approved_official_contact_still_obeys_length_guard():
    template = f"support@example.com {'x' * 5000}"
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text=template,
        source="knowledge",
    )
    result = run_final_guard(
        decision,
        "telegram",
        approved_official_contact_reply=template,
    )
    assert result.action is ReplyAction.HANDOFF
    assert "GUARD_TOO_LONG" in result.reason_codes


def test_private_auto_reply_with_pii_is_also_blocked():
    d = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text="请联系 a@b.com",
        reply_visibility=Visibility.PRIVATE,
    )
    assert run_final_guard(d, "telegram").action is ReplyAction.HANDOFF


def test_private_draft_with_pii_keeps_review_behavior():
    d = ReplyDecision(
        action=ReplyAction.DRAFT,
        reply_text="请人工核对 a@b.com",
        reply_visibility=Visibility.PRIVATE,
    )
    assert run_final_guard(d, "telegram") is d


@pytest.mark.parametrize(
    "reply_text",
    (
        "请联系 a@b.com",
        "Visit https://support.example.com/help",
        "Visit www.example.com/help",
        "Visit support.example.com",
        "Follow @WikiFXSupport",
        "Telegram ID: wikifx_support",
        "Phone: +12345",
        "tel:+12345",
        "WhatsApp: +12345",
        "Telegram ID: +12345",
        "Feishu ID: +12345",
        "Lark ID: +12345",
        "飞书账号：+12345",
        "微信号：wikifx123",
        "Customer service hotline: 12345",
        "Customer service number is 12345",
        "Call us at 12345",
        "客服电话是 12345",
        "客服热线为 12345",
        "请致电 1234",
        "9555 客服热线",
    ),
)
def test_contact_like_output_is_blocked(reply_text):
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text=reply_text,
        reply_visibility=Visibility.PUBLIC,
    )
    result = run_final_guard(decision, "telegram")
    assert result.action is ReplyAction.HANDOFF
    assert "GUARD_PII_LEAK" in result.reason_codes


def test_pii_with_space_separators_blocked():
    # 分隔符绕过：空格分组手机号在归一化后仍应命中长数字串
    d = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text="我的手机是 138 0013 8000",
        reply_visibility=Visibility.PUBLIC,
    )
    out = run_final_guard(d, "telegram")
    assert out.action is ReplyAction.HANDOFF
    assert "GUARD_PII_LEAK" in out.reason_codes


def test_pii_with_dash_separators_blocked():
    d = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text="卡号 8812-3456-7890",
        reply_visibility=Visibility.PUBLIC,
    )
    out = run_final_guard(d, "telegram")
    assert out.action is ReplyAction.HANDOFF
    assert "GUARD_PII_LEAK" in out.reason_codes


@pytest.mark.parametrize(
    "reply_text",
    (
        "3 天内回复，工单号 12345",
        "HTTP status 404 indicates the page was not found.",
        "Version 1.2.3 is now available.",
        "The price is USD @ 5 per unit.",
        "客服将在 3 天内回复。",
        "Please read the support article in the help center.",
        "Call us at 5 pm tomorrow.",
        "Customer service is available 24 hours.",
        "The phone model is 12345.",
        "Contact Energy and LINE Corporation are broker names in this example.",
        "Broker license 12345 is listed for reference.",
        "The risk score is 9555 out of 10000.",
        "The malformed values https:// and www. are not contact destinations.",
    ),
)
def test_contact_like_detector_avoids_bounded_false_positives(reply_text):
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text=reply_text,
        reply_visibility=Visibility.PUBLIC,
    )
    assert run_final_guard(decision, "telegram").action is ReplyAction.AUTO_REPLY


def test_too_long_downgraded():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="x" * 5000)
    out = run_final_guard(d, "telegram")
    assert out.action is ReplyAction.HANDOFF
    assert "GUARD_TOO_LONG" in out.reason_codes


def test_feishu_text_limit_is_4000_characters():
    at_limit = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="x" * 4000)
    over_limit = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="x" * 4001)

    assert run_final_guard(at_limit, "feishu").action is ReplyAction.AUTO_REPLY
    rejected = run_final_guard(over_limit, "feishu")
    assert rejected.action is ReplyAction.HANDOFF
    assert "GUARD_TOO_LONG" in rejected.reason_codes


def test_empty_reply_blocked():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="  ")
    assert run_final_guard(d, "telegram").action is ReplyAction.HANDOFF


def test_clean_reply_passes():
    d = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="您好，请提供订单号。")
    assert run_final_guard(d, "telegram").action is ReplyAction.AUTO_REPLY


def test_expected_language_and_equivalent_localized_time_fact_pass():
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text="退款通常需要 3 到 5 个工作日。",
    )
    result = run_final_guard(
        decision,
        "telegram",
        expected_reply_language="zh-Hans",
        approved_knowledge_reply="Refunds usually take 3–5 business days.",
    )
    assert result.action is ReplyAction.AUTO_REPLY
    assert result.reply_language in {"zh", "zh-Hans"}


def test_wrong_reply_language_is_blocked():
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text="Refunds usually take three business days.",
    )
    result = run_final_guard(
        decision,
        "telegram",
        expected_reply_language="zh-Hans",
        approved_knowledge_reply="Refunds usually take three business days.",
    )
    assert result.action is ReplyAction.HANDOFF
    assert "GUARD_LANGUAGE_MISMATCH" in result.reason_codes


def test_changed_time_unit_is_blocked():
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text="退款通常需要 3 到 5 个小时。",
    )
    result = run_final_guard(
        decision,
        "telegram",
        expected_reply_language="zh-Hans",
        approved_knowledge_reply="Refunds usually take 3–5 business days.",
    )
    assert result.action is ReplyAction.HANDOFF
    assert "GUARD_KNOWLEDGE_FACT_MISMATCH" in result.reason_codes


def test_changed_protected_entity_is_blocked():
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text="根据已批准的服务政策，OtherFX 通常会在 3 个工作日内回复客户的问题。",
    )
    result = run_final_guard(
        decision,
        "telegram",
        expected_reply_language="zh-Hans",
        approved_knowledge_reply="WikiFX usually replies within 3 business days.",
    )
    assert result.action is ReplyAction.HANDOFF
    assert "GUARD_KNOWLEDGE_ENTITY_MISMATCH" in result.reason_codes


@pytest.mark.parametrize(
    "template",
    ("support@example.com", "https://support.example.com/contact"),
)
def test_language_neutral_approved_contact_verbatim_can_pass_in_english(template):
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text=template,
        source="knowledge",
    )
    result = run_final_guard(
        decision,
        "telegram",
        approved_official_contact_reply=template,
        approved_knowledge_reply=template,
        expected_reply_language="en",
    )
    assert result.action is ReplyAction.AUTO_REPLY
    assert result.reply_language == "en"


def test_fact_guard_allows_localized_word_order_but_blocks_value_role_swap():
    approved = "Pay USD 10 within 3 days."
    valid = run_final_guard(
        ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="请在 3 天内支付 10 美元。"),
        "telegram",
        expected_reply_language="zh-Hans",
        approved_knowledge_reply=approved,
    )
    assert valid.action is ReplyAction.AUTO_REPLY

    invalid = run_final_guard(
        ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="请在 10 天内支付 3 美元。"),
        "telegram",
        expected_reply_language="zh-Hans",
        approved_knowledge_reply=approved,
    )
    assert invalid.action is ReplyAction.HANDOFF
    assert "GUARD_KNOWLEDGE_FACT_MISMATCH" in invalid.reason_codes


def test_fact_guard_uses_target_locale_for_grouping_separators():
    equivalent = run_final_guard(
        ReplyDecision(
            action=ReplyAction.AUTO_REPLY,
            reply_text="Die Gebühr beträgt 1.000 USD.",
        ),
        "telegram",
        expected_reply_language="de",
        approved_knowledge_reply="The fee is USD 1,000.",
    )
    assert equivalent.action is ReplyAction.AUTO_REPLY

    changed = run_final_guard(
        ReplyDecision(
            action=ReplyAction.AUTO_REPLY,
            reply_text="Die Gebühr beträgt 1.000 USD.",
        ),
        "telegram",
        expected_reply_language="de",
        approved_knowledge_reply="The fee is USD 1.",
    )
    assert changed.action is ReplyAction.HANDOFF
    assert "GUARD_KNOWLEDGE_FACT_MISMATCH" in changed.reason_codes
