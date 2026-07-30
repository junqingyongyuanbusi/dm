import pytest

from social_reply.application.reply_decision.jobs import snapshot_from_dict, snapshot_to_dict
from social_reply.application.reply_decision.pipeline import DecisionSnapshot, run_decision_pipeline
from social_reply.domain.messages.canonical import ChannelType
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, Visibility
from social_reply.domain.reply.llm import StubLLMClient


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
    )
    context = captured["context"]
    assert context.text == "邮箱 [REDACTED_EMAIL]"
    assert context.history == (("user", "手机号 [REDACTED_NUMBER]"),)


async def test_human_active_forces_ignore():
    d = await run_decision_pipeline(
        _snap(state="HUMAN_ACTIVE"), llm=StubLLMClient(), killswitch=_OpenSwitch()
    )
    assert d.action is ReplyAction.IGNORE
    assert "HUMAN_ACTIVE" in d.reason_codes


async def test_draft_only_downgrades_auto_reply_to_draft():
    d = await run_decision_pipeline(
        _snap(state="BOT_DRAFT_ONLY"), llm=StubLLMClient(), killswitch=_OpenSwitch()
    )
    assert d.action is ReplyAction.DRAFT


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


async def test_killswitch_error_fails_closed_to_draft():
    # kill switch 是安全控制：无法验证急停状态时必须 fail-closed（降级草稿，不外发）
    d = await run_decision_pipeline(_snap(), llm=StubLLMClient(), killswitch=_BrokenSwitch())
    assert d.action is ReplyAction.DRAFT
    assert "KILLSWITCH_UNAVAILABLE" in d.reason_codes


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
