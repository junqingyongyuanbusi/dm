import hashlib

import pytest

from social_reply.domain.reply.decision import (
    ReplyAction,
    ReplyDecision,
    Visibility,
)
from social_reply.domain.reply.guard import (
    LANGUAGE_POLICY_REVIEW,
    protected_entities,
    run_final_guard,
)


def test_non_auto_reply_passes_through_untouched():
    d = ReplyDecision(action=ReplyAction.HANDOFF, reason_codes=("RISK_WORD",))
    assert run_final_guard(d, "telegram") is d


def test_review_policy_does_not_reclassify_existing_handoff():
    decision = ReplyDecision(action=ReplyAction.HANDOFF, reason_codes=("RISK_WORD",))
    assert (
        run_final_guard(
            decision,
            "telegram",
            expected_reply_language="ja",
            language_policy=LANGUAGE_POLICY_REVIEW,
        )
        is decision
    )


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
    assert result.reply_text is None
    assert "GUARD_LANGUAGE_SCRIPT_MISMATCH" in result.reason_codes


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


# 生产实测：飞书渠道英语全部放行、非英语逐条转人工，根因是实体提取与语言校验都把
# 「必须逐字保留的拉丁实体」处理错了。以下用例锁住两侧行为，防止再次回归。
_MULTI_ENTITY_ANSWER = (
    "This may be possible, but running many EAs can increase CPU "
    "and memory usage and may affect VPS performance."
)


@pytest.mark.parametrize(
    ("language", "customer_text", "reply_text"),
    (
        # 无空格文字系统：EA/CPU/VPS 两侧都不成立 \b 词边界，曾一个实体都提不出来。
        (
            "ja",
            "1つのVPSで複数のEAを稼働させることはできますか？",
            "可能ですが、多くのEAを稼働させるとCPUとメモリの使用量が増加し、"
            "VPSのパフォーマンスに影響する可能性があります。",
        ),
        (
            "zh-Hans",
            "一台VPS可以同时运行多个EA吗？",
            "这可能可以，但同时运行多个EA会增加CPU和内存的占用，并可能影响VPS的性能。",
        ),
        (
            "ko",
            "하나의 VPS에서 여러 EA를 실행할 수 있나요?",
            "가능할 수 있지만, 많은 EA를 실행하면 CPU와 메모리 사용량이 증가하고 "
            "VPS 성능에 영향을 줄 수 있습니다.",
        ),
        # 有空格的语言：英语原文的复数 EAs 提不出来、译文的 EA 提得出来，集合照样不等。
        (
            "es",
            "¿Puedo usar varios EA en un solo VPS?",
            "Es posible, pero ejecutar muchos EA puede aumentar el uso de CPU "
            "y memoria y afectar el rendimiento del VPS.",
        ),
        (
            "ru",
            "Можно ли запускать несколько EA на одном VPS?",
            "Это возможно, но запуск многих EA может увеличить использование CPU "
            "и памяти и повлиять на производительность VPS.",
        ),
    ),
)
def test_faithful_translation_preserving_acronyms_passes(language, customer_text, reply_text):
    decision = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text=reply_text)
    result = run_final_guard(
        decision,
        "feishu",
        expected_reply_language=language,
        approved_knowledge_reply=_MULTI_ENTITY_ANSWER,
        customer_text=customer_text,
    )
    assert result.action is ReplyAction.AUTO_REPLY, result.reason_codes


@pytest.mark.parametrize(
    ("language", "customer_text", "reply_text"),
    (
        # 拉丁实体占比高的正确译文：实体本身不携带语种信息，不得据此判成回错语言。
        (
            "ja",
            "どのチャートを使えばいいですか？",
            "マーケットチャートには MT4、MT5、または TradingView のご利用をおすすめします。",
        ),
        (
            "zh-Hans",
            "推荐用什么看图软件？",
            "我们建议使用 MT4、MT5 或 TradingView 查看市场图表。",
        ),
        (
            "ko",
            "어떤 차트를 쓰면 좋나요?",
            "시장 차트에는 MT4, MT5 또는 TradingView 사용을 권장합니다.",
        ),
    ),
)
def test_latin_heavy_translation_is_not_treated_as_wrong_language(
    language, customer_text, reply_text
):
    decision = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text=reply_text)
    result = run_final_guard(
        decision,
        "feishu",
        expected_reply_language=language,
        approved_knowledge_reply=(
            "We recommend using MT4, MT5, or TradingView for your market charts."
        ),
        customer_text=customer_text,
    )
    assert result.action is ReplyAction.AUTO_REPLY, result.reason_codes


@pytest.mark.parametrize(
    ("reply_text", "expected_code"),
    (
        # 实体被替换/凭空新增，仍必须拦住——放宽边界不得削弱防篡改。
        (
            "可能ですが、多くのEAを稼働させるとGPUとメモリの使用量が増加し、"
            "VPSのパフォーマンスに影響する可能性があります。",
            "GUARD_KNOWLEDGE_ENTITY_MISMATCH",
        ),
        (
            "可能ですが、多くのEAを稼働させるとCPUとメモリの使用量が増加し、"
            "VPSのパフォーマンスに影響します。OtherFXにお問い合わせください。",
            "GUARD_KNOWLEDGE_ENTITY_MISMATCH",
        ),
        # 实体齐全但整段是另一种语言：语言闸门照旧拦住。
        (
            "Es posible, pero ejecutar muchos EA puede aumentar el uso de CPU "
            "y memoria y afectar el rendimiento del VPS.",
            "GUARD_LANGUAGE_SCRIPT_MISMATCH",
        ),
    ),
)
def test_entity_tampering_and_wrong_language_still_blocked(reply_text, expected_code):
    decision = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text=reply_text)
    result = run_final_guard(
        decision,
        "feishu",
        expected_reply_language="ja",
        approved_knowledge_reply=_MULTI_ENTITY_ANSWER,
        customer_text="1つのVPSで複数のEAを稼働させることはできますか？",
    )
    assert result.action is ReplyAction.HANDOFF
    assert expected_code in result.reason_codes


def test_versioned_product_name_substitution_is_blocked():
    """MT4→MT5 必须拦住：版本号同时是事实 token，由事实闸门先命中。"""
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text="WikiFXは通常、MT5で3営業日以内に返信します。",
    )
    result = run_final_guard(
        decision,
        "feishu",
        expected_reply_language="ja",
        approved_knowledge_reply="WikiFX usually replies within 3 business days on MT4.",
        customer_text="WikiFXの返信はどのくらいかかりますか？",
    )
    assert result.action is ReplyAction.HANDOFF
    assert "GUARD_KNOWLEDGE_FACT_MISMATCH" in result.reason_codes


def test_acronym_entities_are_extracted_without_ascii_word_boundaries():
    """实体提取的口径：无空格语境下照样提得出，缩写复数归一，版本号并入实体本体。"""
    assert set(protected_entities("多くのEAを稼働させるとCPUとVPSに影響します")) == {
        "EA",
        "CPU",
        "VPS",
    }
    assert set(protected_entities("running many EAs can affect VPS")) == {"EA", "VPS"}
    assert set(protected_entities("MT4 と MT5 に対応しています")) == {"MT4", "MT5"}


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


# --- Task 2：任意语言支持（脚本泛化 / 时间单位分层 / lenient 校验）---

_APPROVED_DAYS = "Refunds take 3 to 5 business days."


def _auto(text: str) -> ReplyDecision:
    return ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text=text)


def test_script_generalization_unblocks_languages_missing_from_the_table():
    # 马其顿语能被 detect_language 正确判出，但不在 _LANGUAGE_ALLOWED_SCRIPTS 里，
    # 旧行为把西里尔字母全判成越界。客户原文的文字系统应当补进允许集合。
    customer = "Колку време трае враќањето на средствата?"
    reply = "Враќањето на средствата трае од 3 до 5 работни дена."
    blocked = run_final_guard(_auto(reply), "telegram", expected_reply_language="mk")
    assert blocked.action is ReplyAction.HANDOFF
    assert "GUARD_LANGUAGE_SCRIPT_MISMATCH" in blocked.reason_codes

    allowed = run_final_guard(
        _auto(reply), "telegram", expected_reply_language="mk", customer_text=customer
    )
    assert allowed.action is ReplyAction.AUTO_REPLY


def test_customer_script_only_widens_never_narrows():
    # 日语是 kana+han 混合书写，客户原文主导脚本可能只有 kana；
    # 绝不能因此把回复里的汉字判成越界。
    result = run_final_guard(
        _auto("返金には通常3〜5営業日かかります。"),
        "telegram",
        expected_reply_language="ja",
        customer_text="返金はいつ反映されますか？",
    )
    assert result.action is ReplyAction.AUTO_REPLY


def test_unrecognized_time_unit_is_marked_unverified_not_blocked():
    # 德语 Werktage 不在 _TIME_UNIT_PATTERNS 词表内：数值一致即放行，
    # 单位标注为未校验，交 grounding verifier 兜底。
    result = run_final_guard(
        _auto("Rückerstattungen dauern 3 bis 5 Werktage."),
        "telegram",
        expected_reply_language="de",
        approved_knowledge_reply=_APPROVED_DAYS,
    )
    assert result.action is ReplyAction.AUTO_REPLY
    assert "FACT_UNIT_UNVERIFIED" in result.reason_codes


def test_recognized_time_unit_swap_is_still_blocked():
    # 候选侧识别出了单位却与批准答案不同——确凿篡改，降级不适用。
    result = run_final_guard(
        _auto("退款需要 3 到 5 个小时。"),
        "telegram",
        expected_reply_language="zh-Hans",
        approved_knowledge_reply=_APPROVED_DAYS,
    )
    assert result.action is ReplyAction.HANDOFF
    assert "GUARD_KNOWLEDGE_FACT_MISMATCH" in result.reason_codes


def test_unit_downgrade_does_not_let_number_tampering_through():
    # 单位降级只放宽单位，数值层永远严格。
    result = run_final_guard(
        _auto("Rückerstattungen dauern 30 bis 5 Werktage."),
        "telegram",
        expected_reply_language="de",
        approved_knowledge_reply=_APPROVED_DAYS,
    )
    assert result.action is ReplyAction.HANDOFF
    assert "GUARD_KNOWLEDGE_FACT_MISMATCH" in result.reason_codes


def test_covered_language_still_verifies_units_strictly():
    result = run_final_guard(
        _auto("返金は3〜5営業日かかります。"),
        "telegram",
        expected_reply_language="ja",
        approved_knowledge_reply=_APPROVED_DAYS,
        customer_text="返金はいつ反映されますか？",
    )
    assert result.action is ReplyAction.AUTO_REPLY
    assert "FACT_UNIT_UNVERIFIED" not in result.reason_codes


def test_lenient_verification_accepts_language_the_detector_cannot_confirm():
    # 尼泊尔语：detect_language 主动 fail-closed，严格校验必然拦下；
    # 语种由模型判定时退到文字系统一致性。
    customer = "म पैसा कसरी निकाल्न सक्छु?"
    reply = "फिर्ता गर्न 3 देखि 5 कार्य दिन लाग्छ।"
    strict = run_final_guard(_auto(reply), "telegram", expected_reply_language="ne")
    assert strict.action is ReplyAction.HANDOFF

    lenient = run_final_guard(
        _auto(reply),
        "telegram",
        expected_reply_language="ne",
        customer_text=customer,
        language_verification="lenient",
    )
    assert lenient.action is ReplyAction.AUTO_REPLY
    assert lenient.reply_language == "ne"  # 不能留 und，否则投递层硬拒绝
    assert "LANGUAGE_MODEL_ATTESTED" in lenient.reason_codes


def test_lenient_verification_still_blocks_wrong_writing_system():
    result = run_final_guard(
        _auto("Refunds take 3 to 5 business days."),
        "telegram",
        expected_reply_language="ne",
        customer_text="म पैसा कसरी निकाल्न सक्छु?",
        language_verification="lenient",
    )
    assert result.action is ReplyAction.HANDOFF
    assert "GUARD_LANGUAGE_SCRIPT_MISMATCH" in result.reason_codes


@pytest.mark.parametrize(
    "reply",
    [
        "Hello! Welcome to our trading community. How can we help you today?",
        "Thank you! We update our charts daily to help you stay ahead of the market.",
        "Of course! Are you looking for market analysis, broker reviews, or beginner guides?",
        "Yes! All our educational content on this page is 100% free. Happy learning!",
        "Great idea! We have an interactive carousel guide on drawing key levels coming out soon.",
        "Spot on! Always double-check the broker's official website URL against the regulator.",
    ],
)
def test_短句开头的英文答案不再被误判为语言不符(reply: str) -> None:
    # 确定性检测对单句问候没有判别力（"Hello" 的 top1 是 st，"Sure" 是 fr），把
    # "判不出"当违规会让 716 条已发布英文答案里的 83 条永远只能转人工。
    result = run_final_guard(
        _auto(reply),
        "telegram",
        expected_reply_language="en",
        customer_text="hello",
        approved_knowledge_reply=reply,
    )
    assert result.action is ReplyAction.AUTO_REPLY, result.reason_codes
    assert result.reply_language == "en"


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        # 整条回复就是另一种语言——整条检测这一层拦住。
        ("Bonjour, nous sommes ravis de vous accueillir dans notre communaute.", "whole"),
        ("Hola, bienvenido a nuestra comunidad de inversores.", "whole"),
        # 长外语句子混进英语回复——片段可靠地检出另一种语言。
        ("Hello! Nous vous invitons a consulter notre guide complet pour les debutants.", "frag"),
        ("Sure. Bitte beachten Sie, dass unsere Analysen zu Bildungszwecken dienen.", "frag"),
        # 非拉丁短片段——片段级文字系统检查兜住，短到判不出语种也拦得下。
        ("你好。Our team reviews every broker on the list before publishing.", "script"),
        ("Спасибо. Our team reviews every broker on the list before publishing.", "script"),
        ("Merci beaucoup. Our team reviews every broker on the list before publishing.", "frag"),
    ],
)
def test_回错语言仍然被拦住(reply: str, reason: str) -> None:
    result = run_final_guard(
        _auto(reply), "telegram", expected_reply_language="en", customer_text="hello"
    )
    assert result.action is ReplyAction.HANDOFF, reason
    expected_reason = (
        "GUARD_LANGUAGE_SCRIPT_MISMATCH" if reason == "script" else "GUARD_LANGUAGE_MISMATCH"
    )
    assert expected_reason in result.reason_codes


# --- Language observations are review signals, not deterministic safety facts ---


@pytest.mark.parametrize(
    ("expected_language", "reply"),
    (("en", "Hello"), ("es", "Hola"), ("fr", "Bonjour")),
)
def test_uncertain_short_greeting_is_preserved_as_private_review_draft(
    expected_language: str,
    reply: str,
) -> None:
    result = run_final_guard(
        _auto(reply),
        "telegram",
        expected_reply_language=expected_language,
        customer_text=reply,
        language_policy=LANGUAGE_POLICY_REVIEW,
    )

    assert result.action is ReplyAction.DRAFT
    assert result.reply_text == reply
    assert result.reply_visibility is Visibility.PRIVATE
    assert result.reply_language == "und"
    assert "GUARD_LANGUAGE_MISMATCH" in result.reason_codes


def test_japanese_reply_with_full_width_punctuation_and_latin_products_passes() -> None:
    approved = "Keep the MT4 and VPS settings unchanged and continue using them."
    reply = "MT4；VPSの設定は変更せず、そのままご利用ください。"

    result = run_final_guard(
        _auto(reply),
        "feishu",
        expected_reply_language="ja",
        approved_knowledge_reply=approved,
        approved_knowledge_protected_values=("MT4", "VPS"),
        customer_text="MT4とVPSの設定は変更する必要がありますか？",
        language_policy=LANGUAGE_POLICY_REVIEW,
    )

    assert result.action is ReplyAction.AUTO_REPLY, result.reason_codes


@pytest.mark.parametrize(
    ("customer_text", "reply"),
    (
        ("Hello 你好", "Hello，你好。"),
        ("はい、OKです", "はい、OKです。"),
    ),
)
def test_mirror_user_keeps_scripts_present_in_unresolved_customer(
    customer_text: str,
    reply: str,
) -> None:
    result = run_final_guard(
        _auto(reply),
        "telegram",
        expected_reply_language="mirror-user",
        customer_text=customer_text,
        language_policy=LANGUAGE_POLICY_REVIEW,
    )

    assert result.action is ReplyAction.DRAFT, result.reason_codes
    assert result.reply_text == reply
    assert result.reply_visibility is Visibility.PRIVATE
    assert "GUARD_LANGUAGE_SCRIPT_MISMATCH" not in result.reason_codes


def test_mirror_user_still_rejects_script_absent_from_unresolved_customer() -> None:
    result = run_final_guard(
        _auto("Спасибо за обращение."),
        "telegram",
        expected_reply_language="mirror-user",
        customer_text="Hello 你好",
        language_policy=LANGUAGE_POLICY_REVIEW,
    )

    assert result.action is ReplyAction.HANDOFF
    assert result.reply_text is None
    assert "GUARD_LANGUAGE_SCRIPT_MISMATCH" in result.reason_codes


def test_mixed_script_conflict_remains_a_hard_failure_in_review_mode() -> None:
    reply = "Hello. Спасибо за обращение в службу поддержки."

    result = run_final_guard(
        _auto(reply),
        "telegram",
        expected_reply_language="en",
        customer_text="Hello",
        language_policy=LANGUAGE_POLICY_REVIEW,
    )

    assert result.action is ReplyAction.HANDOFF
    assert result.reply_text is None
    assert "GUARD_LANGUAGE_SCRIPT_MISMATCH" in result.reason_codes


def test_lenient_script_mismatch_remains_a_hard_failure_in_review_mode() -> None:
    reply = "Refunds take 3 to 5 business days."

    result = run_final_guard(
        _auto(reply),
        "telegram",
        expected_reply_language="ja",
        customer_text="返金には何日かかりますか？",
        language_verification="lenient",
        language_policy=LANGUAGE_POLICY_REVIEW,
    )

    assert result.action is ReplyAction.HANDOFF
    assert result.reply_text is None
    assert "GUARD_LANGUAGE_SCRIPT_MISMATCH" in result.reason_codes


@pytest.mark.parametrize(
    ("reply", "approved", "protected_values", "expected_reason"),
    (
        (
            "返金には5営業日かかります。",
            "Refunds take 3 business days.",
            (),
            "GUARD_KNOWLEDGE_FACT_MISMATCH",
        ),
        (
            "GoogleはVPSをサポートしています。",
            "Meta supports VPS.",
            ("Meta",),
            "GUARD_KNOWLEDGE_ENTITY_MISMATCH",
        ),
        (
            "Bonjour, contact alice@example.com.",
            None,
            (),
            "GUARD_PII_LEAK",
        ),
    ),
)
def test_hard_tampering_still_hands_off_and_clears_reply_in_review_mode(
    reply: str,
    approved: str | None,
    protected_values: tuple[str, ...],
    expected_reason: str,
) -> None:
    result = run_final_guard(
        _auto(reply),
        "telegram",
        expected_reply_language="ja",
        approved_knowledge_reply=approved,
        approved_knowledge_protected_values=protected_values,
        customer_text="返金について教えてください。",
        language_policy=LANGUAGE_POLICY_REVIEW,
    )

    assert result.action is ReplyAction.HANDOFF
    assert result.reply_text is None
    assert expected_reason in result.reason_codes
    assert "GUARD_LANGUAGE_MISMATCH" not in result.reason_codes


def test_protected_entities_use_knowledge_bound_values_without_global_brand_table() -> None:
    assert protected_entities("Meta and VPS", protected_values=("Meta",)) == ("VPS", "Meta")
    assert protected_entities("Meta and VPS") == ("VPS",)
    assert protected_entities("Metadata and VPS", protected_values=("Meta",)) == ("VPS",)


def test_arbitrary_knowledge_bound_protected_value_is_required_verbatim() -> None:
    approved = "Use the Acme portal for this request."
    valid = run_final_guard(
        _auto("Use the Acme portal for this request."),
        "telegram",
        approved_knowledge_reply=approved,
        approved_knowledge_protected_values=("Acme",),
    )
    replaced = run_final_guard(
        _auto("Use the Example portal for this request."),
        "telegram",
        approved_knowledge_reply=approved,
        approved_knowledge_protected_values=("Acme",),
    )

    assert valid.action is ReplyAction.AUTO_REPLY
    assert replaced.action is ReplyAction.HANDOFF
    assert replaced.reply_text is None
    assert "GUARD_KNOWLEDGE_ENTITY_MISMATCH" in replaced.reason_codes


def test_localization_protected_value_failure_remains_hard_in_review_mode() -> None:
    reply = "公式ポータルをご利用ください。"
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text=reply,
        source="knowledge_localization",
    )
    result = run_final_guard(
        decision,
        "telegram",
        expected_reply_language="ja",
        approved_localization_text=reply,
        approved_localization_text_hash=hashlib.sha256(reply.encode()).hexdigest(),
        approved_localization_protected_values=("Acme",),
        customer_text="公式ポータルはどこですか？",
        language_policy=LANGUAGE_POLICY_REVIEW,
    )

    assert result.action is ReplyAction.HANDOFF
    assert result.reply_text is None
    assert "GUARD_LOCALIZATION_PROTECTED_VALUE_MISMATCH" in result.reason_codes
    assert "GUARD_LANGUAGE_MISMATCH" not in result.reason_codes
