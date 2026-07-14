import re
from dataclasses import replace

from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, Visibility

# 账户号/长数字串、邮箱——公开回复禁止回显（PLAN.md §五 Final Guard）
_PII_PATTERNS = (
    re.compile(r"\d{6,}"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
)
_MAX_TEXT_LENGTH = {"telegram": 4096, "facebook": 2000, "instagram": 1000}
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
    if (
        decision.reply_visibility is Visibility.PUBLIC
        and any(p.search(text) for p in _PII_PATTERNS)
    ):
        return _downgrade(decision, "GUARD_PII_LEAK")
    if len(text) > _MAX_TEXT_LENGTH.get(platform, _DEFAULT_MAX):
        return _downgrade(decision, "GUARD_TOO_LONG")
    return decision
