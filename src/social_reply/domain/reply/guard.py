import re
from dataclasses import replace

from social_reply.domain.platform_accounts import PLATFORM_CAPABILITY_SPECS
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, Visibility

# 账户号/长数字串、邮箱——公开回复禁止回显，发送给外部 LLM 前也需脱敏。
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# 连续或带常见分组符的 6 位以上数字，例如 123456、138 0013 8000。
_GROUPED_DIGITS = re.compile(r"(?<!\d)\d(?:[\s\-–—.·]*\d){5,}(?!\d)")


def _has_pii(text: str) -> bool:
    return bool(_GROUPED_DIGITS.search(text) or _EMAIL.search(text))


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
        reason_codes=decision.reason_codes + (code,),
        source="guard",
    )


def run_final_guard(decision: ReplyDecision, platform: str) -> ReplyDecision:
    """纯确定性输出闸门；任一项失败降级为 handoff 并记录 reason_code。
    仅对 auto_reply 生效——其它 action 原样返回。"""
    if decision.action is not ReplyAction.AUTO_REPLY:
        return decision
    text = decision.reply_text or ""
    if not text.strip():
        return _downgrade(decision, "GUARD_EMPTY")
    if decision.reply_visibility is Visibility.PUBLIC and _has_pii(text):
        return _downgrade(decision, "GUARD_PII_LEAK")
    if len(text) > _MAX_TEXT_LENGTH.get(platform, _DEFAULT_MAX):
        return _downgrade(decision, "GUARD_TOO_LONG")
    return decision
