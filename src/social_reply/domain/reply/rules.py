from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, RiskLevel

# Legacy production rules stay unchanged while the feature-gated multilingual path uses the
# narrower semantic list below.
RISK_WORDS = ("诈骗", "无法出金", "无法提现", "律师", "起诉", "退款", "账户冻结", "冻结")

MULTILINGUAL_RISK_PHRASES = (
    "诈骗",
    "无法出金",
    "无法提现",
    "账户冻结",
    "退款未到账",
    "退款没到账",
    "查退款状态",
    "退款争议",
    "律师",
    "起诉",
    "scam",
    "cannot withdraw",
    "unable to withdraw",
    "account frozen",
    "frozen account",
    "refund not received",
    "refund overdue",
    "refund dispute",
    "check my refund",
    "where is my refund",
    "lawyer",
    "lawsuit",
    "estafa",
    "no puedo retirar",
    "cuenta congelada",
    "reembolso no recibido",
    "disputa de reembolso",
    "abogado",
    "demanda",
    "arnaque",
    "impossible de retirer",
    "compte bloqué",
    "remboursement non reçu",
    "litige de remboursement",
    "avocat",
    "poursuite",
    "fraude",
    "não consigo retirar",
    "conta bloqueada",
    "reembolso não recebido",
    "disputa de reembolso",
    "advogado",
    "processo",
    "詐欺",
    "出金できない",
    "口座凍結",
    "返金されない",
    "返金が届かない",
    "弁護士",
    "訴訟",
    "사기",
    "출금할 수 없",
    "계정 동결",
    "환불을 받지 못",
    "환불 분쟁",
    "변호사",
    "소송",
    "мошенничество",
    "не могу вывести",
    "счет заморожен",
    "возврат не получен",
    "спор о возврате",
    "адвокат",
    "احتيال",
    "لا أستطيع السحب",
    "تجميد الحساب",
    "لم أستلم المبلغ المسترد",
    "نزاع استرداد",
    "محامي",
    "โกง",
    "ถอนเงินไม่ได้",
    "บัญชีถูกระงับ",
    "ยังไม่ได้รับเงินคืน",
    "ข้อพิพาทการคืนเงิน",
    "ทนาย",
)


def _handoff(reason: str) -> ReplyDecision:
    return ReplyDecision(
        action=ReplyAction.HANDOFF,
        risk_level=RiskLevel.HIGH,
        reason_codes=(reason,),
        source="rule",
    )


def apply_rules(text: str | None) -> ReplyDecision | None:
    """Legacy deterministic rule path retained unchanged while the new feature flag is off."""
    if text is None or not text.strip():
        return ReplyDecision(
            action=ReplyAction.HANDOFF,
            reason_codes=("EMPTY_OR_NON_TEXT",),
            source="rule",
        )
    if any(word in text for word in RISK_WORDS):
        return _handoff("RISK_WORD")
    return None


def apply_multilingual_rules(text: str | None) -> ReplyDecision | None:
    """Fail closed on risk/failure/escalation semantics without blocking ordinary FAQ topics."""
    if text is None or not text.strip():
        return ReplyDecision(
            action=ReplyAction.HANDOFF,
            reason_codes=("EMPTY_OR_NON_TEXT",),
            source="rule",
        )
    normalized = text.casefold()
    if any(phrase.casefold() in normalized for phrase in MULTILINGUAL_RISK_PHRASES):
        return _handoff("MULTILINGUAL_RISK")
    return None
