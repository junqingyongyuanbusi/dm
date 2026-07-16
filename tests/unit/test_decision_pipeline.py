from social_reply.application.reply_decision.pipeline import (
    DecisionSnapshot,
    run_decision_pipeline,
)
from social_reply.domain.reply.decision import ReplyAction
from social_reply.domain.reply.llm import StubLLMClient


class _OpenSwitch:
    async def is_disabled(self, brand_id, account_id):
        return False


class _ClosedSwitch:
    async def is_disabled(self, brand_id, account_id):
        return True


class _BrokenSwitch:
    async def is_disabled(self, brand_id, account_id):
        raise ConnectionError("redis down")


def _snap(state="BOT_ACTIVE", text="请问怎么改邮箱"):
    return DecisionSnapshot(
        text=text, platform="telegram", brand_id="b1", account_id="acc1",
        conversation_key="telegram:acc1:9", automation_state=state, state_version=1,
    )


async def test_bot_active_normal_question_auto_replies_via_llm():
    d = await run_decision_pipeline(_snap(), llm=StubLLMClient(), killswitch=_OpenSwitch())
    assert d.action is ReplyAction.AUTO_REPLY
    assert "STUB_LLM" in d.reason_codes


async def test_human_active_forces_ignore():
    d = await run_decision_pipeline(_snap(state="HUMAN_ACTIVE"), llm=StubLLMClient(),
                                    killswitch=_OpenSwitch())
    assert d.action is ReplyAction.IGNORE
    assert "HUMAN_ACTIVE" in d.reason_codes


async def test_draft_only_downgrades_auto_reply_to_draft():
    d = await run_decision_pipeline(_snap(state="BOT_DRAFT_ONLY"), llm=StubLLMClient(),
                                    killswitch=_OpenSwitch())
    assert d.action is ReplyAction.DRAFT


async def test_killswitch_forces_draft():
    d = await run_decision_pipeline(_snap(), llm=StubLLMClient(), killswitch=_ClosedSwitch())
    assert d.action is ReplyAction.DRAFT
    assert "KILLSWITCH" in d.reason_codes


async def test_risk_word_handoff_before_llm():
    d = await run_decision_pipeline(_snap(text="我要起诉你们"), llm=StubLLMClient(),
                                    killswitch=_OpenSwitch())
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
        _snap(text="hello"), llm=_MustNotCall(), killswitch=_OpenSwitch(),
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
        _snap(text="你们是不是诈骗"), llm=StubLLMClient(), killswitch=_OpenSwitch(),
        knowledge=("问：Scam?\n答：check regulators",), verbatim_reply="check regulators",
    )
    assert d.action is ReplyAction.HANDOFF
    assert "RISK_WORD" in d.reason_codes
