import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import replace

from social_reply.domain.platform_accounts import PLATFORM_CAPABILITY_SPECS
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, Visibility
from social_reply.domain.reply.language import (
    expected_scripts_for,
    reply_language_matches,
    reply_script_matches,
)

# 语言校验强度。strict：语种由确定性检测判定，可复核，沿用完整的语言身份断言。
# lenient：语种由模型判定，确定性检测复核不了它（罕见语言、或近亲语言二次确认），
# 退到文字系统一致性，语义忠实度由 grounding verifier 承担——投递层已强制
# grounding_verified is True 才放行，安全链条不因此断裂。
LANGUAGE_VERIFICATION_STRICT = "strict"
LANGUAGE_VERIFICATION_LENIENT = "lenient"

# Language identity is an observation, not a deterministic safety fact. Keep the old hard
# behavior available during rollout, while review mode preserves the candidate for human review.
LANGUAGE_POLICY_LEGACY_HARD = "legacy_hard"
LANGUAGE_POLICY_REVIEW = "review"

# Account numbers, long digit strings, and email addresses must not be echoed.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Six or more digits, either continuous or separated with common grouping characters.
_GROUPED_DIGITS = re.compile(r"(?<!\d)\d(?:[\s\-–—.·]*\d){5,}(?!\d)")
_URL = re.compile(r"(?i)(?<![A-Z0-9_])(?:https?://|www\.)[A-Z0-9][^\s<>()]*")
_BARE_DOMAIN = re.compile(
    r"(?i)(?<![A-Z0-9_@.-])"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,24}"
    r"(?::\d{2,5})?(?:/[^\s<>()]*)?(?![A-Z0-9_.-])"
)
_HANDLE = re.compile(r"(?i)(?<![A-Z0-9_.+@-])@[A-Z0-9_][A-Z0-9_.-]{0,31}(?![A-Z0-9_@.-])")
_MESSAGING_ID = re.compile(
    r"(?ix)"
    r"(?:whats\s*app|we\s*chat|wechat|weixin|telegram|signal|skype|line|qq"
    r"|messenger|discord|viber|kakao\s*talk|kakaotalk|feishu|lark|微信|微訊|飞书|飛書)"
    r"\s*(?:(?:id|user(?:name)?|handle|number|no\.?|账号|帳號|号码|號碼|号|號)"
    r"\s*[:：]?\s*|[:：]\s*)"
    r"(?:@|\+)?[A-Z0-9][A-Z0-9_.+-]{1,63}"
)
_SERVICE_NUMBER_CONTEXT = (
    r"(?:customer\s+service(?:\s+(?:line|number))?"
    r"|service\s+(?:hotline|line|number)"
    r"|support\s+(?:hotline|line|number)"
    r"|contact\s+(?:number|line)|hotline|call|phone|tel(?:ephone)?"
    r"|客服(?:热线|熱線|电话|電話|号码|號碼|号|號)?"
    r"|服务热线|服務熱線|服务电话|服務電話|联系电话|聯繫電話"
    r"|联系热线|聯繫熱線|致电|致電|拨打|撥打)"
)
_SHORT_SERVICE_NUMBER = re.compile(
    rf"(?ix)(?:"
    rf"{_SERVICE_NUMBER_CONTEXT}"
    rf"\s*(?:(?:number|no\.?|号码|號碼|号|號)\s*)?"
    rf"(?:us\s+|me\s+)?(?:(?:is|at|on)\s+|(?:为|為|是)\s*)?[:：]?\s*"
    rf"(?<!\d)\+?\d{{3,5}}(?!\d)"
    rf"|(?<!\d)\+?\d{{3,5}}(?!\d)\s*"
    rf"(?:customer\s+service|service\s+(?:hotline|line)|support\s+(?:hotline|line)"
    rf"|hotline|客服热线|客服熱線|服务热线|服務熱線)"
    rf")"
)
_NUMBER_TOKEN = re.compile(
    r"(?P<currency>[$€£¥])?\s*(?P<number>\d+(?:[.,]\d+)?)(?P<percent>\s*[%％])?"
)
_TIME_UNIT_PATTERNS = (
    (
        "day",
        re.compile(
            r"(?i)business\s+days?|days?|工作日|天|jours?|días?|dias?|営業日|일|วัน|дн(?:я|ей)?|أيام?|दिन|दिवस|hari|දින|ວັນ|ថ្ងៃ|ရက်"
        ),
    ),
    (
        "hour",
        re.compile(r"(?i)hours?|小时|小時|heures?|horas?|時間|시간|ชั่วโมง|час(?:а|ов)?|ساعات?|घंटे|jam|පැය|ຊົ່ວໂມງ|နာရီ")
    ),
    (
        "week",
        re.compile(r"(?i)weeks?|周|週|semaines?|semanas?|週間|주|สัปดาห์|недел(?:я|и|ь)|أسابيع?|सप्ताह|minggu|සතිය|ອາທິດ|အပတ်")
    ),
    (
        "month",
        re.compile(r"(?i)months?|月|mois|meses?|か月|개월|เดือน|месяц(?:а|ев)?|أشهر?|महीने|bulan|මාස|ເດືອນ|လ"),
    ),
    (
        "year",
        re.compile(r"(?i)years?|年|ans?|años?|anos?|年間|년|ปี|лет|سنوات?|वर्ष|tahun|වසර|ປີ|နှစ်"),
    ),
)
_CURRENCY_PATTERNS = (
    ("USD", re.compile(r"(?i)USD|US\$|美元|dollars?|dólares?")),
    ("EUR", re.compile(r"(?i)EUR|€|欧元|歐元|euros?")),
    ("GBP", re.compile(r"(?i)GBP|£|英镑|英鎊|pounds?")),
    ("JPY", re.compile(r"(?i)JPY|日元|円|yen")),
    ("CNY", re.compile(r"(?i)CNY|RMB|人民币|人民幣|(?<!日)元")),
    ("USDT", re.compile(r"(?i)USDT|Tether")),
)
# 实体边界不能用 \b：Unicode 下 \w 包含汉字、假名、韩文与泰文，"多くのEAを" 里 EA
# 两侧都不成立词边界，日中韩泰这类不加空格的语言因此一个实体都提不出来——空集与英语
# 原文的 {CPU, VPS} 必然不等，每条非英语回复都被误判成实体篡改。改用「两侧不是 ASCII
# 字母」的显式边界；尾随数字并入实体本体，使 MT4/MT5 这类版本号可比对而非截成 MT。
_PROTECTED_ENTITY = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z]{2,}\d*s?|[A-Z][a-z]+[A-Z][A-Za-z]*)(?![A-Za-z])"
)
_FACT_SEPARATOR = re.compile(r"(?i)\b(?:and|or|ou|y|e)\b|[;,，；]|或|和|以及")


def has_contact_like(text: str) -> bool:
    return any(
        pattern.search(text)
        for pattern in (
            _GROUPED_DIGITS,
            _EMAIL,
            _URL,
            _BARE_DOMAIN,
            _HANDLE,
            _MESSAGING_ID,
            _SHORT_SERVICE_NUMBER,
        )
    )


def contact_values(text: str) -> tuple[str, ...]:
    values: list[str] = []
    for pattern in (
        _GROUPED_DIGITS,
        _EMAIL,
        _URL,
        _BARE_DOMAIN,
        _HANDLE,
        _MESSAGING_ID,
        _SHORT_SERVICE_NUMBER,
    ):
        values.extend(match.group(0) for match in pattern.finditer(text))
    return tuple(dict.fromkeys(values))


def _ascii_digits(text: str) -> str:
    normalized: list[str] = []
    for char in text:
        if unicodedata.category(char) == "Nd":
            normalized.append(str(unicodedata.digit(char)))
        elif char == "٫":
            normalized.append(".")
        elif char == "٬":
            normalized.append(",")
        else:
            normalized.append(char)
    return "".join(normalized)


_DECIMAL_COMMA_LANGUAGES = {"de", "es", "fr", "pt", "it", "nl", "pl", "ru", "tr"}


def _normalize_number(value: str, language: str) -> str:
    primary_language = language.split("-", 1)[0].casefold()
    if primary_language in _DECIMAL_COMMA_LANGUAGES:
        if "." in value and "," not in value:
            before, after = value.split(".", 1)
            value = before + after if len(after) == 3 else f"{before}.{after}"
        elif "," in value and "." not in value:
            value = value.replace(",", ".")
        elif "," in value and "." in value:
            value = value.replace(".", "").replace(",", ".")
    else:
        if "," in value and "." not in value:
            before, after = value.split(",", 1)
            value = before + after if len(after) == 3 else f"{before}.{after}"
        elif "," in value and "." in value:
            value = value.replace(",", "")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value.lstrip("0") or "0"


def _context_label(
    patterns: tuple[tuple[str, re.Pattern[str]], ...],
    context: str,
) -> str:
    return next((label for label, pattern in patterns if pattern.search(context)), "")


def factual_tokens(text: str, *, language: str = "en") -> tuple[tuple[str, str, bool, str], ...]:
    normalized = _ascii_digits(text)
    matches = list(_NUMBER_TOKEN.finditer(normalized))
    tokens: list[tuple[str, str, bool, str]] = []
    for index, match in enumerate(matches):
        previous_end = matches[index - 1].end() if index > 0 else 0
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        prefix = normalized[max(previous_end, match.start() - 16) : match.start()]
        suffix = normalized[match.end() : min(next_start, match.end() + 24)]
        currency_context = f"{prefix[-12:]} {match.group(0)} {suffix[:12]}"
        currency = _context_label(_CURRENCY_PATTERNS, currency_context)
        time_context = suffix[:24]
        if not currency and match.group("currency") == "$":
            currency = "USD"
        tokens.append(
            (
                _normalize_number(match.group("number"), language),
                currency,
                bool(match.group("percent")),
                _context_label(_TIME_UNIT_PATTERNS, time_context),
            )
        )
    return tuple(tokens)


def _normalize_entity(value: str) -> str:
    """归一化缩写复数：英语原文写 EAs、译文写 EA，两者必须算同一个实体。

    不归一时英语侧提不出 EAs（尾随小写 s 破坏全大写形态），译文侧却提得出 EA，
    集合比对必然不等——与文字系统无关，拉丁语系的译文同样被误判。
    """
    if len(value) > 2 and value.endswith("s") and value[:-1].isupper():
        return value[:-1]
    return value


def protected_entities(
    text: str,
    *,
    protected_values: tuple[str, ...] = (),
) -> tuple[str, ...]:
    regex_entities = [
        entity
        for entity in (_normalize_entity(match) for match in _PROTECTED_ENTITY.findall(text))
        if entity not in {"USD", "EUR", "GBP", "CNY", "RMB", "JPY", "USDT"}
    ]
    knowledge_entities = [
        value
        for value in protected_values
        if value
        and re.search(
            (r"(?<![A-Za-z0-9])" if value[0].isascii() and value[0].isalnum() else "")
            + re.escape(value)
            + (r"(?![A-Za-z0-9])" if value[-1].isascii() and value[-1].isalnum() else ""),
            text,
        )
    ]
    return tuple(dict.fromkeys([*regex_entities, *knowledge_entities]))


def redact_pii(text: str) -> str:
    """最小化发送给外部 LLM 的自由文本，不修改数据库中的原始会话记录。"""
    redacted = _EMAIL.sub("[REDACTED_EMAIL]", text)
    return _GROUPED_DIGITS.sub("[REDACTED_NUMBER]", redacted)


_MAX_TEXT_LENGTH = {
    platform.value: spec.max_text_length for platform, spec in PLATFORM_CAPABILITY_SPECS.items()
}
_DEFAULT_MAX = 2000


def _downgrade(decision: ReplyDecision, code: str) -> ReplyDecision:
    return replace(
        decision,
        action=ReplyAction.HANDOFF,
        reply_text=None,
        reason_codes=decision.reason_codes + (code,),
        source="guard",
    )


def _apply_language_mismatch(
    decision: ReplyDecision,
    code: str,
    *,
    language_policy: str,
) -> ReplyDecision:
    if language_policy != LANGUAGE_POLICY_REVIEW:
        return _downgrade(decision, code)
    return replace(
        decision,
        action=ReplyAction.DRAFT,
        reply_visibility=Visibility.PRIVATE,
        reason_codes=decision.reason_codes + (code,),
        source="guard",
    )


def _fact_tokens_match(candidate: str, approved: str, language: str) -> tuple[bool, bool]:
    """比对候选回复与英语批准答案的事实 token。

    返回 (数值层是否一致, 时间单位是否已校验)。

    数值、货币、百分号与语言无关，永远严格比对——这是防篡改的核心。时间单位依赖
    `_TIME_UNIT_PATTERNS` 的逐语言词表，而该词表覆盖不全（实测德语、意大利语、
    越南语、土耳其语、荷兰语、波兰语、瑞典语的"工作日"都识别不出）。对这些语言
    按"未校验"处理而非误判为篡改：调用方记 reason code，由 grounding verifier 兜底
    （已用对抗样本验证其能拦下单位与数值篡改）。

    注意只有"候选侧完全识别不出单位"才降级；候选侧识别出了但与批准答案不同
    （如 days → 小时），仍是确凿的篡改，照旧拦截。
    """
    candidate_tokens = factual_tokens(candidate, language=language)
    approved_tokens = factual_tokens(approved, language="en")
    if Counter(token[:3] for token in candidate_tokens) != Counter(
        token[:3] for token in approved_tokens
    ):
        return False, True
    candidate_units = Counter(token[3] for token in candidate_tokens if token[3])
    approved_units = Counter(token[3] for token in approved_tokens if token[3])
    if candidate_units == approved_units:
        return True, True
    if not candidate_units and approved_units:
        return True, False
    return False, True


def run_hard_output_guard(
    decision: ReplyDecision,
    platform: str,
    *,
    approved_official_contact_reply: str | None = None,
    expected_reply_language: str = "und",
    approved_knowledge_reply: str | None = None,
    approved_knowledge_protected_values: tuple[str, ...] = (),
    approved_localization_text: str | None = None,
    approved_localization_text_hash: str | None = None,
    approved_localization_protected_values: tuple[str, ...] = (),
) -> ReplyDecision:
    """Run only deterministic output-safety checks.

    Any failure hands off and clears the candidate. Language identity is deliberately excluded so
    an uncertain language detector cannot skip fact, entity, contact, or PII checks.
    """
    if decision.action is not ReplyAction.AUTO_REPLY:
        return decision
    text = decision.reply_text or ""
    if not text.strip():
        return _downgrade(decision, "GUARD_EMPTY")
    if len(text) > _MAX_TEXT_LENGTH.get(platform, _DEFAULT_MAX):
        return _downgrade(decision, "GUARD_TOO_LONG")
    if decision.source == "knowledge_localization":
        if approved_localization_text is None or approved_localization_text_hash is None:
            return _downgrade(decision, "GUARD_LOCALIZATION_PROVENANCE_MISSING")
        if text != approved_localization_text:
            return _downgrade(decision, "GUARD_LOCALIZATION_TEXT_MISMATCH")
    approved_localization = decision.source == "knowledge_localization"
    if approved_localization:
        if hashlib.sha256(text.encode()).hexdigest() != approved_localization_text_hash:
            return _downgrade(decision, "GUARD_LOCALIZATION_HASH_MISMATCH")
        if any(value not in text for value in approved_localization_protected_values):
            return _downgrade(decision, "GUARD_LOCALIZATION_PROTECTED_VALUE_MISMATCH")
    approved_contact = (
        decision.source == "knowledge"
        and approved_official_contact_reply is not None
        and text == approved_official_contact_reply
    ) or approved_localization
    if approved_knowledge_reply is not None:
        facts_ok, units_verified = _fact_tokens_match(
            text, approved_knowledge_reply, expected_reply_language
        )
        if not facts_ok:
            return _downgrade(decision, "GUARD_KNOWLEDGE_FACT_MISMATCH")
        if not units_verified:
            decision = replace(
                decision, reason_codes=decision.reason_codes + ("FACT_UNIT_UNVERIFIED",)
            )
        if set(
            protected_entities(text, protected_values=approved_knowledge_protected_values)
        ) != set(
            protected_entities(
                approved_knowledge_reply,
                protected_values=approved_knowledge_protected_values,
            )
        ):
            return _downgrade(decision, "GUARD_KNOWLEDGE_ENTITY_MISMATCH")
    if has_contact_like(text) and not approved_contact:
        return _downgrade(decision, "GUARD_PII_LEAK")
    return decision


def run_language_observation_guard(
    decision: ReplyDecision,
    *,
    approved_official_contact_reply: str | None = None,
    expected_reply_language: str = "und",
    approved_knowledge_reply: str | None = None,
    approved_knowledge_protected_values: tuple[str, ...] = (),
    approved_localization_protected_values: tuple[str, ...] = (),
    customer_text: str | None = None,
    language_verification: str = LANGUAGE_VERIFICATION_STRICT,
    language_policy: str = LANGUAGE_POLICY_LEGACY_HARD,
) -> ReplyDecision:
    """Observe output language after deterministic and semantic safety checks.

    ``legacy_hard`` preserves the historical handoff behavior. ``review`` records the same signal
    on a private draft without discarding the candidate text.
    """
    if decision.action is not ReplyAction.AUTO_REPLY or expected_reply_language == "und":
        return decision

    text = decision.reply_text or ""
    approved_contact = (
        decision.source == "knowledge"
        and approved_official_contact_reply is not None
        and text == approved_official_contact_reply
    ) or decision.source == "knowledge_localization"
    neutral_terms = (
        protected_entities(
            approved_knowledge_reply,
            protected_values=approved_knowledge_protected_values,
        )
        if approved_knowledge_reply is not None
        else ()
    ) + approved_localization_protected_values
    extra_allowed_scripts = expected_scripts_for(
        customer_text,
        include_all=expected_reply_language == "mirror-user",
    )
    if language_verification == LANGUAGE_VERIFICATION_LENIENT:
        # The model-attested language remains routing metadata. A writing-system conflict is still
        # deterministic enough to reject in every policy; only language identity is reviewable.
        decision = replace(
            decision,
            reply_language=expected_reply_language,
            reason_codes=decision.reason_codes + ("LANGUAGE_MODEL_ATTESTED",),
        )
        if not reply_script_matches(
            expected_reply_language,
            text,
            extra_allowed_scripts=extra_allowed_scripts,
            neutral_terms=neutral_terms,
        ):
            return _downgrade(
                decision,
                "GUARD_LANGUAGE_SCRIPT_MISMATCH",
            )
        return decision

    # Protected values must be copied verbatim, so they carry no useful language evidence.
    language_ok, observed_language = reply_language_matches(
        expected_reply_language,
        text,
        extra_allowed_scripts=extra_allowed_scripts,
        neutral_terms=neutral_terms,
    )
    if observed_language == "und" and approved_contact:
        observed_language = expected_reply_language
        language_ok = True
    decision = replace(decision, reply_language=observed_language)
    if not reply_script_matches(
        expected_reply_language,
        text,
        extra_allowed_scripts=extra_allowed_scripts,
        neutral_terms=neutral_terms,
    ):
        return _downgrade(decision, "GUARD_LANGUAGE_SCRIPT_MISMATCH")
    if not language_ok:
        return _apply_language_mismatch(
            decision,
            "GUARD_LANGUAGE_MISMATCH",
            language_policy=language_policy,
        )
    return decision


def run_final_guard(
    decision: ReplyDecision,
    platform: str,
    *,
    approved_official_contact_reply: str | None = None,
    expected_reply_language: str = "und",
    approved_knowledge_reply: str | None = None,
    approved_knowledge_protected_values: tuple[str, ...] = (),
    approved_localization_text: str | None = None,
    approved_localization_text_hash: str | None = None,
    approved_localization_protected_values: tuple[str, ...] = (),
    customer_text: str | None = None,
    language_verification: str = LANGUAGE_VERIFICATION_STRICT,
    language_policy: str = LANGUAGE_POLICY_LEGACY_HARD,
) -> ReplyDecision:
    """Compatibility wrapper for callers that do not run a semantic verifier between phases."""
    decision = run_hard_output_guard(
        decision,
        platform,
        approved_official_contact_reply=approved_official_contact_reply,
        expected_reply_language=expected_reply_language,
        approved_knowledge_reply=approved_knowledge_reply,
        approved_knowledge_protected_values=approved_knowledge_protected_values,
        approved_localization_text=approved_localization_text,
        approved_localization_text_hash=approved_localization_text_hash,
        approved_localization_protected_values=approved_localization_protected_values,
    )
    return run_language_observation_guard(
        decision,
        approved_official_contact_reply=approved_official_contact_reply,
        expected_reply_language=expected_reply_language,
        approved_knowledge_reply=approved_knowledge_reply,
        approved_knowledge_protected_values=approved_knowledge_protected_values,
        approved_localization_protected_values=approved_localization_protected_values,
        customer_text=customer_text,
        language_verification=language_verification,
        language_policy=language_policy,
    )
