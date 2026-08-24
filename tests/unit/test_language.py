import pytest

from social_reply.domain.reply.language import (
    assess_knowledge_language,
    detect_language,
    detect_language_from_history,
    is_deterministically_verifiable,
    languages_match,
    reply_language_matches,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("你好，請問如何申請退款？", "zh-Hant"),
        ("你好，请问如何申请退款？", "zh-Hans"),
        ("支持支付宝吗", "zh-Hans"),
        ("退款多久", "zh"),
        ("你好", "zh"),
        ("返金はいつ反映されますか？", "ja"),
        ("환불은 언제 처리되나요?", "ko"),
        ("¿Cómo puedo retirar dinero?", "es"),
        ("Comment puis-je retirer mon argent ?", "fr"),
        ("Como posso retirar dinheiro?", "pt"),
        ("Hello, how can I get a refund?", "en"),
        ("Как вывести деньги со счета?", "ru"),
        ("ฉันจะถอนเงินได้อย่างไร?", "th"),
        ("كيف يمكنني سحب الأموال؟", "ar"),
        ("چگونه پول برداشت کنم؟", "fa"),
        ("میں رقم کیسے نکال سکتا ہوں؟", "ur"),
        ("Πώς μπορώ να κάνω ανάληψη χρημάτων;", "el"),
        ("मैं पैसे कैसे निकालूं?", "hi"),
        ("माझे पैसे कसे काढायचे आहेत?", "mr"),
        ("আমি কীভাবে টাকা তুলতে পারি?", "bn"),
        ("איך אני יכול למשוך כסף?", "he"),
        ("ਮੈਂ ਪੈਸੇ ਕਿਵੇਂ ਕੱਢ ਸਕਦਾ ਹਾਂ?", "pa"),
        ("હું પૈસા કેવી રીતે ઉપાડી શકું?", "gu"),
        ("நான் பணத்தை எவ்வாறு எடுக்கலாம்?", "ta"),
        ("నేను డబ్బును ఎలా ఉపసంహరించుకోవాలి?", "te"),
        ("Ինչպե՞ս կարող եմ գումար հանել։", "hy"),
        ("როგორ შემიძლია თანხის გატანა?", "ka"),
    ],
)
def test_detect_language_for_supported_customer_messages(text, expected):
    assert detect_language(text).tag == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "👍",
        "123456",
        "OK",
        "Refund",
        "WikiFX",
        "返金希望",  # Han-only text is ambiguous between Chinese and Japanese.
        "中文消息の",  # One Kana character must not override the dominant script.
        "म पैसा कसरी निकाल्न सक्छु?",  # Nepali is outside Lingua's supported set.
        "মই কেনেকৈ ধন উলিয়াব পাৰোঁ?",  # Assamese is outside Lingua's supported set.
        "ווי קען איך צוריקציען געלט?",  # Yiddish is outside Lingua's supported set.
    ],
)
def test_short_ambiguous_or_unsupported_text_is_unknown(text):
    assert detect_language(text).tag == "und"


def test_single_foreign_script_character_does_not_override_dominant_language():
    assert detect_language("This English support message includes 한").tag == "en"


def test_history_lookback_prefers_the_newest_reliable_customer_message():
    result = detect_language_from_history(
        (
            ("user", "你好，我想了解退款政策。"),
            ("user", "Comment puis-je obtenir un remboursement ?"),
            ("assistant", "This English bot reply must not decide the customer language."),
        )
    )
    assert result is not None
    assert result.tag == "fr"
    assert result.source == "recent_user_history"


def test_history_lookback_ignores_messages_beyond_the_window():
    # 只看最近 3 条客户消息：更早的语言不该无限期地影响判定。
    result = detect_language_from_history(
        (
            ("user", "Comment puis-je obtenir un remboursement ?"),
            ("user", "OK"),
            ("user", "👍"),
            ("user", "[REDACTED_EMAIL]"),
        )
    )
    assert result is None


def test_chinese_script_only_lookback_refines_variant():
    result = detect_language_from_history(
        (("user", "請問退款通常需要幾天？"),), chinese_script_only=True
    )
    assert result is not None
    assert result.tag == "zh-Hant"
    assert result.source == "recent_user_history"


def test_chinese_script_only_lookback_rejects_other_languages():
    # 当前消息已确定是中文时，历史只能用来定简繁，绝不能把它判成别的语言。
    assert (
        detect_language_from_history(
            (("user", "Comment puis-je obtenir un remboursement ?"),),
            chinese_script_only=True,
        )
        is None
    )


def test_redaction_placeholders_do_not_turn_history_into_english():
    assert detect_language_from_history((("user", "[REDACTED_EMAIL] [REDACTED_NUMBER]"),)) is None


def test_history_lookback_without_reliable_customer_messages_returns_none():
    assert detect_language_from_history(()) is None
    assert detect_language_from_history((("assistant", "How can we help you today?"),)) is None


def test_deterministic_verifiability_splits_guard_strictness():
    # detect_language 能产出的标签才能被输出闸门严格复核。
    for tag in ("en", "ja", "ko", "zh", "zh-Hans", "zh-Hant", "ru", "ar", "hi", "mr", "th"):
        assert is_deterministically_verifiable(tag) is True
    # 这些语言 detect_language 主动 fail-closed 或压根不在候选表里，严格校验必然误杀。
    for tag in ("ne", "am", "ti", "yi", "und"):
        assert is_deterministically_verifiable(tag) is False


def test_languages_match_primary_language_and_chinese_script():
    assert languages_match("pt-BR", "pt") is True
    assert languages_match("en", "fr") is False
    assert languages_match("und", "en") is False
    assert languages_match("zh-Hans", "zh-Hans") is True
    assert languages_match("zh-Hant", "zh-Hant") is True
    assert languages_match("zh-Hans", "zh-Hant") is False
    assert languages_match("zh", "zh-Hans") is True
    assert languages_match("zh-Hant", "zh") is True


def test_reply_language_rejects_mixed_natural_language_sentences():
    assert (
        reply_language_matches(
            "zh-Hans",
            "退款通常需要 3 到 5 个工作日。Please contact support if delayed.",
        )[0]
        is False
    )
    assert (
        reply_language_matches(
            "es",
            "El reembolso tarda de 3 a 5 días. Please contact support if delayed.",
        )[0]
        is False
    )


def test_url_does_not_swallow_adjacent_second_language_text():
    assert reply_language_matches("en", "Please see https://example.com请联系客服")[0] is False
    assert (
        reply_language_matches(
            "en",
            "Please see https://example.comيرجى الاتصال بالدعم",
        )[0]
        is False
    )


def test_reply_language_allows_language_neutral_contact_tokens():
    assert (
        reply_language_matches("en", "Official support is available at support@example.com")[0]
        is True
    )
    assert reply_language_matches("de", "Weitere Informationen: https://example.com/a.b")[0] is True



@pytest.mark.parametrize(
    ("language", "sample"),
    [
        ("zh-Hans", "退款通常需要3到5个工作日。"),
        ("ja", "返金には通常3〜5営業日かかります。"),
        ("ko", "환불은 보통 3~5영업일이 걸립니다."),
        ("hi", "रिफंड में आमतौर पर 3 से 5 कार्यदिवस लगते हैं।"),
        ("si", "මුදල් ආපසු ලබා ගැනීමට සාමාන්‍යයෙන් වැඩ කරන දින 3 සිට 5 දක්වා ගත වේ."),
        ("lo", "ການຄືນເງິນໃຊ້ເວລາ 3 ຫາ 5 ວັນເຮັດວຽກ."),
        ("km", "ការបង្វិលប្រាក់វិញត្រូវចំណាយពេល ៣ ដល់ ៥ ថ្ងៃធ្វើការ។"),
        ("my", "ငွေပြန်အမ်းရန် အလုပ်လုပ်ရက် ၃ မှ ၅ ရက်ကြာနိုင်သည်။"),
    ],
)
def test_reply_language_allows_detected_writing_systems(language, sample):
    assert reply_language_matches(language, sample)[0] is True
    assert reply_language_matches(language, "Refunds take 3 to 5 business days.")[0] is False


def test_ethiopic_script_is_ambiguous_and_fails_closed():
    sample = "የገንዘብ ተመላሽ ከ3 እስከ 5 የስራ ቀናት ይወስዳል።"
    assert detect_language(sample).tag == "und"
    assert reply_language_matches("am", sample)[0] is False



def test_knowledge_language_assessment_separates_detection_from_confirmation():
    assert assess_knowledge_language(
        "Could you please explain how long a refund usually takes in business days?",
        "Refunds usually take 3 to 5 business days.",
    ) == ("en", "english")
    assert assess_knowledge_language("Refund", "退款通常需要 3 到 5 个工作日。") == (
        "zh-Hans",
        "non_english",
    )
    assert assess_knowledge_language("How can I contact support?", "support@example.com") == (
        "en",
        "unknown",
    )
