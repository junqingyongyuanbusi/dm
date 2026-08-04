import re
from dataclasses import replace

from social_reply.domain.platform_accounts import PLATFORM_CAPABILITY_SPECS
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision

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
    r"|messenger|discord|viber|kakao\s*talk|kakaotalk|微信|微訊)"
    r"\s*(?:(?:id|user(?:name)?|handle|number|no\.?|账号|帳號|号码|號碼|号|號)"
    r"\s*[:：]?\s*|[:：]\s*)"
    r"@?[A-Z0-9][A-Z0-9_.+-]{1,63}"
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
    rf"\s*(?:(?:number|no\.?|号码|號碼|号|號)\s*)?[:：]?\s*"
    rf"(?<!\d)\d{{3,5}}(?!\d)"
    rf"|(?<!\d)\d{{3,5}}(?!\d)\s*"
    rf"(?:customer\s+service|service\s+(?:hotline|line)|support\s+(?:hotline|line)"
    rf"|hotline|客服热线|客服熱線|服务热线|服務熱線)"
    rf")"
)


def _has_contact_like(text: str) -> bool:
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


def run_final_guard(
    decision: ReplyDecision,
    platform: str,
    *,
    approved_official_contact_reply: str | None = None,
) -> ReplyDecision:
    """纯确定性输出闸门；任一项失败降级为 handoff 并记录 reason_code。
    仅对 auto_reply 生效——其它 action 原样返回。"""
    if decision.action is not ReplyAction.AUTO_REPLY:
        return decision
    text = decision.reply_text or ""
    if not text.strip():
        return _downgrade(decision, "GUARD_EMPTY")
    if len(text) > _MAX_TEXT_LENGTH.get(platform, _DEFAULT_MAX):
        return _downgrade(decision, "GUARD_TOO_LONG")
    if _has_contact_like(text):
        approved_contact = (
            decision.source == "knowledge"
            and approved_official_contact_reply is not None
            and text.strip() == approved_official_contact_reply.strip()
        )
        if not approved_contact:
            return _downgrade(decision, "GUARD_PII_LEAK")
    return decision
