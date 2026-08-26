import pytest

from social_reply.application.reply_decision.jobs import snapshot_from_dict, snapshot_to_dict
from social_reply.application.reply_decision.pipeline import DecisionSnapshot, run_decision_pipeline
from social_reply.domain.messages.canonical import ChannelType
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, Visibility
from social_reply.domain.reply.guard import LANGUAGE_POLICY_REVIEW
from social_reply.domain.reply.llm import (
    APPROVED_VERBATIM_SENTINEL,
    RAGVerificationResult,
    StubLLMClient,
)
from social_reply.domain.reply.voice import DEFAULT_VOICE_PREFERENCES


class _OpenSwitch:
    async def is_disabled(self, brand_id, account_id, tenant_id="default"):
        return False


class _ClosedSwitch:
    async def is_disabled(self, brand_id, account_id, tenant_id="default"):
        return True


class _BrokenSwitch:
    async def is_disabled(self, brand_id, account_id, tenant_id="default"):
        raise ConnectionError("redis down")


def _snap(state="BOT_ACTIVE", text="请问怎么改邮箱", **overrides):
    values = {
        "text": text,
        "platform": "telegram",
        "tenant_id": "default",
        "brand_id": "b1",
        "account_id": "acc1",
        "conversation_key": "telegram:acc1:9",
        "automation_state": state,
        "state_version": 1,
    }
    values.update(overrides)
    return DecisionSnapshot(**values)


async def test_bot_active_normal_question_auto_replies_via_llm():
    d = await run_decision_pipeline(_snap(), llm=StubLLMClient(), killswitch=_OpenSwitch())
    assert d.action is ReplyAction.AUTO_REPLY
    assert "STUB_LLM" in d.reason_codes


async def test_short_same_language_reply_uses_model_fallback_and_auto_replies():
    class _ShortReplyLLM:
        def __init__(self) -> None:
            self.language_calls: list[str] = []

        async def decide(self, context):
            assert context.target_language == "en"
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="Hello",
                confidence=0.99,
            )

        async def detect_language_tag(self, text):
            self.language_calls.append(text)
            return "en"

    llm = _ShortReplyLLM()
    decision = await run_decision_pipeline(
        _snap(text="Hello"),
        llm=llm,
        killswitch=_OpenSwitch(),
        target_language="en",
        apply_legacy_rules=False,
    )

    assert decision.action is ReplyAction.AUTO_REPLY
    assert decision.reply_language == "en"
    assert llm.language_calls == ["Hello"]


async def test_unresolved_reply_language_becomes_private_draft():
    class _UnresolvedReplyLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="OK",
                confidence=0.99,
            )

        async def detect_language_tag(self, text):
            return None

    decision = await run_decision_pipeline(
        _snap(text="Can you help me?"),
        llm=_UnresolvedReplyLLM(),
        killswitch=_OpenSwitch(),
        target_language="en",
        apply_legacy_rules=False,
    )

    assert decision.action is ReplyAction.DRAFT
    assert decision.reply_text == "OK"
    assert decision.reply_visibility is Visibility.PRIVATE
    assert decision.reply_language == "und"
    assert "GUARD_LANGUAGE_MISMATCH" in decision.reason_codes


async def test_hard_output_guard_runs_before_reply_language_fallback():
    class _UnsafeReplyLLM:
        def __init__(self) -> None:
            self.language_calls: list[str] = []

        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="Contact support@example.com",
                confidence=0.99,
            )

        async def detect_language_tag(self, text):
            self.language_calls.append(text)
            return "en"

    llm = _UnsafeReplyLLM()
    decision = await run_decision_pipeline(
        _snap(text="How can I contact support?"),
        llm=llm,
        killswitch=_OpenSwitch(),
        target_language="en",
        apply_legacy_rules=False,
    )

    assert decision.action is ReplyAction.HANDOFF
    assert decision.reply_text is None
    assert "GUARD_PII_LEAK" in decision.reason_codes
    assert llm.language_calls == []


async def test_llm_context_redacts_current_and_history_pii():
    captured = {}

    class _CaptureLLM:
        async def decide(self, context):
            captured["context"] = context
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="已收到",
                reason_codes=("TEST",),
                source="llm",
            )

    await run_decision_pipeline(
        _snap(text="邮箱 alice@example.com"),
        llm=_CaptureLLM(),
        killswitch=_OpenSwitch(),
        history=(("user", "手机号 138 0013 8000"),),
        voice_preferences=DEFAULT_VOICE_PREFERENCES,
    )
    context = captured["context"]
    assert context.text == "邮箱 [REDACTED_EMAIL]"
    assert context.history == (("user", "手机号 [REDACTED_NUMBER]"),)
    assert context.voice_preferences == DEFAULT_VOICE_PREFERENCES


async def test_human_active_forces_ignore():
    d = await run_decision_pipeline(
        _snap(state="HUMAN_ACTIVE"), llm=StubLLMClient(), killswitch=_OpenSwitch()
    )
    assert d.action is ReplyAction.IGNORE
    assert "HUMAN_ACTIVE" in d.reason_codes


@pytest.mark.parametrize(
    ("email_enabled", "email_auto_reply_enabled", "expected_action"),
    [
        (False, False, ReplyAction.DRAFT),
        (False, True, ReplyAction.DRAFT),
        (True, False, ReplyAction.DRAFT),
        (True, True, ReplyAction.AUTO_REPLY),
    ],
)
async def test_email_decision_gate_requires_enabled_and_auto_reply(
    email_enabled, email_auto_reply_enabled, expected_action
):
    decision = await run_decision_pipeline(
        _snap(platform="email"),
        llm=StubLLMClient(),
        killswitch=_OpenSwitch(),
        email_auto_reply_allowed=email_enabled and email_auto_reply_enabled,
    )
    assert decision.action is expected_action
    if expected_action is ReplyAction.DRAFT:
        assert decision.reply_visibility is Visibility.PRIVATE
        assert decision.reason_codes[-1] == "EMAIL_AUTO_REPLY_DISABLED"


async def test_email_auto_reply_gate_does_not_change_handoff():
    decision = await run_decision_pipeline(
        _snap(platform="email", text="我要起诉你们"),
        llm=StubLLMClient(),
        killswitch=_OpenSwitch(),
        email_auto_reply_allowed=False,
    )
    assert decision.action is ReplyAction.HANDOFF
    assert "EMAIL_AUTO_REPLY_DISABLED" not in decision.reason_codes


async def test_non_email_auto_reply_ignores_email_gate():
    decision = await run_decision_pipeline(
        _snap(platform="telegram"),
        llm=StubLLMClient(),
        killswitch=_OpenSwitch(),
        email_auto_reply_allowed=False,
    )
    assert decision.action is ReplyAction.AUTO_REPLY


async def test_draft_only_downgrades_auto_reply_to_private_draft():
    d = await run_decision_pipeline(
        _snap(state="BOT_DRAFT_ONLY"), llm=StubLLMClient(), killswitch=_OpenSwitch()
    )
    assert d.action is ReplyAction.DRAFT
    assert d.reply_visibility is Visibility.PRIVATE


async def test_killswitch_forces_draft():
    d = await run_decision_pipeline(_snap(), llm=StubLLMClient(), killswitch=_ClosedSwitch())
    assert d.action is ReplyAction.DRAFT
    assert "KILLSWITCH" in d.reason_codes


async def test_risk_word_handoff_before_llm():
    d = await run_decision_pipeline(
        _snap(text="我要起诉你们"), llm=StubLLMClient(), killswitch=_OpenSwitch()
    )
    assert d.action is ReplyAction.HANDOFF
    assert "RISK_WORD" in d.reason_codes


async def test_killswitch_error_fails_closed_to_draft(caplog):
    d = await run_decision_pipeline(_snap(), llm=StubLLMClient(), killswitch=_BrokenSwitch())
    assert d.action is ReplyAction.DRAFT
    assert "KILLSWITCH_UNAVAILABLE" in d.reason_codes
    assert "kill switch lookup failed" in caplog.text


async def test_verbatim_reply_returns_template_text_without_llm():
    class _MustNotCall:
        async def decide(self, context):
            raise AssertionError("verbatim 模式不得调用 LLM")

    d = await run_decision_pipeline(
        _snap(text="hello"),
        llm=_MustNotCall(),
        killswitch=_OpenSwitch(),
        knowledge=("问：Hello\n答：Hello! Welcome to our trading community.",),
        verbatim_reply="Hello! Welcome to our trading community. How can we help you today?",
    )
    assert d.action is ReplyAction.AUTO_REPLY
    assert d.reply_text == "Hello! Welcome to our trading community. How can we help you today?"
    assert d.source == "knowledge"
    assert "KNOWLEDGE_VERBATIM" in d.reason_codes


async def test_verbatim_auto_reply_with_pii_is_blocked_by_final_guard():
    d = await run_decision_pipeline(
        _snap(text="contact details"),
        llm=StubLLMClient(),
        killswitch=_OpenSwitch(),
        verbatim_reply="Email alice@example.com",
    )

    assert d.action is ReplyAction.HANDOFF
    assert d.reply_text is None
    assert "GUARD_PII_LEAK" in d.reason_codes


async def test_approved_official_contact_verbatim_reply_passes_final_guard():
    template = "Official support: support@example.com"
    decision = await run_decision_pipeline(
        _snap(text="official contact"),
        llm=StubLLMClient(),
        killswitch=_OpenSwitch(),
        verbatim_reply=template,
        approved_official_contact_reply=template,
    )
    assert decision.action is ReplyAction.AUTO_REPLY
    assert decision.reply_text == template
    assert decision.source == "knowledge"


async def test_llm_copy_of_approved_contact_is_still_blocked():
    template = "Official support: support@example.com"

    class _CopyingLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text=template,
                source="llm",
            )

    decision = await run_decision_pipeline(
        _snap(text="official contact"),
        llm=_CopyingLLM(),
        killswitch=_OpenSwitch(),
        knowledge=(template,),
        approved_official_contact_reply=template,
    )
    assert decision.action is ReplyAction.HANDOFF
    assert decision.reply_text is None
    assert "GUARD_PII_LEAK" in decision.reason_codes


async def test_risk_word_beats_verbatim_template():
    # 安全规则优先：风险词即使命中模板也必须转人工
    d = await run_decision_pipeline(
        _snap(text="你们是不是诈骗"),
        llm=StubLLMClient(),
        killswitch=_OpenSwitch(),
        knowledge=("问：Scam?\n答：check regulators",),
        verbatim_reply="check regulators",
    )
    assert d.action is ReplyAction.HANDOFF
    assert "RISK_WORD" in d.reason_codes


class _HandoffLLM:
    async def decide(self, context):
        return ReplyDecision(action=ReplyAction.HANDOFF, reason_codes=("OPENAI",), source="llm")


async def test_llm_handoff_remains_handoff_when_bot_active():
    d = await run_decision_pipeline(_snap(), llm=_HandoffLLM(), killswitch=_OpenSwitch())
    assert d.action is ReplyAction.HANDOFF
    assert d.reason_codes == ("OPENAI",)


async def test_llm_handoff_remains_handoff_under_draft_only():
    d = await run_decision_pipeline(
        _snap(state="BOT_DRAFT_ONLY"), llm=_HandoffLLM(), killswitch=_OpenSwitch()
    )
    assert d.action is ReplyAction.HANDOFF
    assert d.reason_codes == ("OPENAI",)


async def test_guard_downgrade_remains_handoff():
    class _PiiLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="请联系 alice@example.com",
                source="llm",
            )

    d = await run_decision_pipeline(_snap(), llm=_PiiLLM(), killswitch=_OpenSwitch())
    assert d.action is ReplyAction.HANDOFF
    assert "GUARD_PII_LEAK" in d.reason_codes
    assert "LLM_HANDOFF_FALLBACK" not in d.reason_codes


async def test_rule_handoff_is_not_converted_to_auto_reply():
    d = await run_decision_pipeline(
        _snap(text="我要起诉你们"), llm=_HandoffLLM(), killswitch=_OpenSwitch()
    )
    assert d.action is ReplyAction.HANDOFF
    assert "LLM_HANDOFF_FALLBACK" not in d.reason_codes


async def test_unsupported_attachment_hands_off_without_calling_llm():
    class _UnexpectedLLM:
        async def decide(self, context):
            raise AssertionError("LLM must not be called for unsupported attachments")

    d = await run_decision_pipeline(
        _snap(text=None, has_unsupported_attachment=True),
        llm=_UnexpectedLLM(),
        killswitch=_OpenSwitch(),
    )
    assert d.action is ReplyAction.HANDOFF
    assert d.reason_codes == ("UNSUPPORTED_ATTACHMENT",)


@pytest.mark.parametrize("state", ["HANDOFF_PENDING", "HUMAN_ACTIVE", "BOT_COOLDOWN", "CLOSED"])
async def test_non_automation_states_do_not_call_llm(state):
    class _UnexpectedLLM:
        async def decide(self, context):
            raise AssertionError("LLM must not be called while automation is paused")

    d = await run_decision_pipeline(
        _snap(state=state), llm=_UnexpectedLLM(), killswitch=_OpenSwitch()
    )
    assert d.action is ReplyAction.IGNORE
    assert d.reason_codes == (state,)


async def test_private_safe_auto_reply_becomes_public_before_guard():
    class _PrivateLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="Safe customer reply",
                reply_visibility=Visibility.PRIVATE,
                source="llm",
            )

    decision = await run_decision_pipeline(_snap(), llm=_PrivateLLM(), killswitch=_OpenSwitch())

    assert decision.action is ReplyAction.AUTO_REPLY
    assert decision.reply_visibility is Visibility.PUBLIC
    assert decision.reason_codes == ("AUTO_REPLY_VISIBILITY_PUBLIC",)


async def test_private_auto_reply_with_pii_hands_off_on_any_channel():
    class _PrivatePiiLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="Email alice@example.com",
                reply_visibility=Visibility.PRIVATE,
                source="llm",
            )

    decision = await run_decision_pipeline(_snap(), llm=_PrivatePiiLLM(), killswitch=_OpenSwitch())

    assert decision.reply_visibility is Visibility.PUBLIC
    assert decision.action is ReplyAction.HANDOFF
    assert "AUTO_REPLY_VISIBILITY_PUBLIC" in decision.reason_codes
    assert "GUARD_PII_LEAK" in decision.reason_codes


@pytest.mark.parametrize(
    ("platform", "reason"),
    [
        ("facebook", "FACEBOOK_COMMENT_PUBLIC"),
        ("instagram", "INSTAGRAM_COMMENT_PUBLIC"),
    ],
)
async def test_meta_comment_forces_public_visibility_before_guard(platform, reason):
    class _PrivateLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="公开答复",
                reply_visibility=Visibility.PRIVATE,
                source="llm",
            )

    decision = await run_decision_pipeline(
        _snap(
            platform=platform,
            channel_type=ChannelType.COMMENT,
        ),
        llm=_PrivateLLM(),
        killswitch=_OpenSwitch(),
    )

    assert decision.reply_visibility is Visibility.PUBLIC
    assert reason in decision.reason_codes


@pytest.mark.parametrize("platform", ["facebook", "instagram"])
async def test_meta_comment_private_pii_is_blocked_after_becoming_public(platform):
    class _PrivatePiiLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="请联系 alice@example.com",
                reply_visibility=Visibility.PRIVATE,
                source="llm",
            )

    decision = await run_decision_pipeline(
        _snap(platform=platform, channel_type=ChannelType.COMMENT),
        llm=_PrivatePiiLLM(),
        killswitch=_OpenSwitch(),
    )

    assert decision.action is ReplyAction.HANDOFF
    assert "GUARD_PII_LEAK" in decision.reason_codes


def test_decision_snapshot_channel_type_round_trips_and_old_jobs_default_to_dm():
    comment = _snap(platform="facebook", channel_type=ChannelType.COMMENT)
    serialized = snapshot_to_dict(comment)

    assert snapshot_from_dict(serialized).channel_type is ChannelType.COMMENT
    serialized.pop("channel_type")
    assert snapshot_from_dict(serialized).channel_type is ChannelType.DM


def test_decision_snapshot_attachment_flag_round_trips():
    snapshot = _snap(has_unsupported_attachment=True)
    assert snapshot_from_dict(snapshot_to_dict(snapshot)).has_unsupported_attachment is True


async def test_approved_verbatim_is_rendered_only_after_safe_action_sentinel():
    class SentinelLLM:
        async def decide(self, context):
            assert context.approved_verbatim_available is True
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text=APPROVED_VERBATIM_SENTINEL,
                confidence=0.99,
            )

    template = "support@example.com"
    decision = await run_decision_pipeline(
        _snap(text="How can I contact official support?"),
        llm=SentinelLLM(),
        killswitch=_OpenSwitch(),
        knowledge=("Official support email: support@example.com",),
        approved_official_contact_reply=template,
        approved_knowledge_reply=template,
        verbatim_after_decision=template,
        target_language="en",
        apply_legacy_rules=False,
    )
    assert decision.action is ReplyAction.AUTO_REPLY
    assert decision.reply_text == template
    assert decision.source == "knowledge"
    assert "KNOWLEDGE_VERBATIM" in decision.reason_codes


async def test_approved_verbatim_missing_sentinel_fails_closed():
    class WrongTextLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="I copied support@example.com",
                confidence=0.99,
            )

    decision = await run_decision_pipeline(
        _snap(text="How can I contact official support?"),
        llm=WrongTextLLM(),
        killswitch=_OpenSwitch(),
        knowledge=("Official support email: support@example.com",),
        approved_official_contact_reply="support@example.com",
        approved_knowledge_reply="support@example.com",
        verbatim_after_decision="support@example.com",
        target_language="en",
        apply_legacy_rules=False,
    )
    assert decision.action is ReplyAction.HANDOFF
    assert decision.reply_text is None
    assert "VERBATIM_SENTINEL_MISSING" in decision.reason_codes


@pytest.mark.parametrize("verifier_result", [False, RuntimeError("verifier unavailable")])
async def test_grounding_verifier_fails_closed(verifier_result):
    class GroundingLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="退款通常需要 3 到 5 个工作日。",
                confidence=0.99,
            )

        async def verify_grounding(self, **kwargs):
            if isinstance(verifier_result, Exception):
                raise verifier_result
            return verifier_result

    decision = await run_decision_pipeline(
        _snap(text="退款多久到账？"),
        llm=GroundingLLM(),
        killswitch=_OpenSwitch(),
        knowledge=("approved evidence",),
        approved_knowledge_reply="Refunds usually take 3–5 business days.",
        target_language="zh-Hans",
        apply_legacy_rules=False,
    )
    assert decision.action is ReplyAction.HANDOFF
    assert decision.grounding_verified is False
    assert decision.grounding_verifier_version == "grounding-v1"
    assert "GUARD_KNOWLEDGE_SEMANTIC_MISMATCH" in decision.reason_codes


async def test_grounding_verifier_accepts_faithful_localization():
    class GroundingLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="退款通常需要 3 到 5 个工作日。",
                confidence=0.99,
            )

        async def verify_grounding(self, **kwargs):
            return True

    decision = await run_decision_pipeline(
        _snap(text="退款多久到账？"),
        llm=GroundingLLM(),
        killswitch=_OpenSwitch(),
        knowledge=("approved evidence",),
        approved_knowledge_reply="Refunds usually take 3–5 business days.",
        target_language="zh-Hans",
        apply_legacy_rules=False,
    )
    assert decision.action is ReplyAction.AUTO_REPLY
    assert decision.grounding_verified is True
    assert decision.grounding_verifier_version == "grounding-v1"


async def test_grounding_verifier_runs_before_language_review_observation():
    events = []

    class GroundingLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="Refunds take 3 business days.",
                confidence=0.99,
            )

        async def verify_grounding(self, **kwargs):
            events.append("grounding")
            return True

    decision = await run_decision_pipeline(
        _snap(text="¿Cuántos días tarda el reembolso?"),
        llm=GroundingLLM(),
        killswitch=_OpenSwitch(),
        knowledge=("approved evidence",),
        approved_knowledge_reply="Refunds take 3 business days.",
        target_language="es",
        apply_legacy_rules=False,
        language_policy=LANGUAGE_POLICY_REVIEW,
    )

    assert events == ["grounding"]
    assert decision.grounding_verified is True
    assert decision.action is ReplyAction.DRAFT
    assert decision.reply_text == "Refunds take 3 business days."
    assert decision.reply_visibility is Visibility.PRIVATE
    assert "GUARD_LANGUAGE_MISMATCH" in decision.reason_codes


@pytest.mark.parametrize("verifier_result", [False, RuntimeError("verifier unavailable")])
async def test_grounding_rejection_beats_language_review_observation(verifier_result):
    class GroundingLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="Refunds take 3 business days.",
                confidence=0.99,
            )

        async def verify_grounding(self, **kwargs):
            if isinstance(verifier_result, Exception):
                raise verifier_result
            return verifier_result

    decision = await run_decision_pipeline(
        _snap(text="返金には何日かかりますか？"),
        llm=GroundingLLM(),
        killswitch=_OpenSwitch(),
        knowledge=("approved evidence",),
        approved_knowledge_reply="Refunds take 3 business days.",
        target_language="ja",
        apply_legacy_rules=False,
        language_policy=LANGUAGE_POLICY_REVIEW,
    )

    assert decision.action is ReplyAction.HANDOFF
    assert decision.reply_text is None
    assert "GUARD_KNOWLEDGE_SEMANTIC_MISMATCH" in decision.reason_codes
    assert "GUARD_LANGUAGE_MISMATCH" not in decision.reason_codes


@pytest.mark.parametrize(
    ("verification", "expected_reason"),
    (
        (
            RAGVerificationResult(relevant=False, faithful=True),
            "GUARD_KNOWLEDGE_RELEVANCE_MISMATCH",
        ),
        (
            RAGVerificationResult(relevant=True, faithful=False),
            "GUARD_KNOWLEDGE_SEMANTIC_MISMATCH",
        ),
    ),
)
async def test_rag_verifier_failure_precedes_language_review(
    verification: RAGVerificationResult,
    expected_reason: str,
) -> None:
    class VerifierLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="Refunds take 3 business days.",
                confidence=0.99,
            )

        async def verify_rag_answer(self, **kwargs):
            assert kwargs["query"] == "返金には何日かかりますか？"
            return verification

        async def verify_grounding(self, **kwargs):
            raise AssertionError("the v2 verifier must take precedence")

    decision = await run_decision_pipeline(
        _snap(text="返金には何日かかりますか？"),
        llm=VerifierLLM(),
        killswitch=_OpenSwitch(),
        knowledge=("approved evidence",),
        approved_knowledge_reply="Refunds take 3 business days.",
        target_language="ja",
        apply_legacy_rules=False,
        language_policy=LANGUAGE_POLICY_REVIEW,
    )

    assert decision.action is ReplyAction.HANDOFF
    assert decision.reply_text is None
    assert expected_reason in decision.reason_codes
    assert "GUARD_LANGUAGE_MISMATCH" not in decision.reason_codes


async def test_hard_guard_failure_skips_grounding_verifier_before_language_review():
    class GroundingLLM:
        async def decide(self, context):
            return ReplyDecision(
                action=ReplyAction.AUTO_REPLY,
                reply_text="返金には5営業日かかります。",
                confidence=0.99,
            )

        async def verify_grounding(self, **kwargs):
            raise AssertionError("hard guard failures must skip grounding")

    decision = await run_decision_pipeline(
        _snap(text="返金には何日かかりますか？"),
        llm=GroundingLLM(),
        killswitch=_OpenSwitch(),
        knowledge=("approved evidence",),
        approved_knowledge_reply="Refunds take 3 business days.",
        target_language="ja",
        apply_legacy_rules=False,
        language_policy=LANGUAGE_POLICY_REVIEW,
    )

    assert decision.action is ReplyAction.HANDOFF
    assert decision.reply_text is None
    assert "GUARD_KNOWLEDGE_FACT_MISMATCH" in decision.reason_codes
    assert decision.grounding_verified is None
