import hashlib
import uuid

import pytest
from sqlalchemy import insert, select, update

import social_reply.infrastructure.queue.broker  # noqa: F401
from social_reply.application.knowledge.localizations import (
    LocalizationDraftInput,
    create_localization_draft,
    publish_localization,
)
from social_reply.application.knowledge.retrieval import KnowledgeRetrievalResult
from social_reply.application.message_delivery import outbox as outbox_module
from social_reply.application.reply_decision import runner
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.connectors import registry
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.infrastructure.database import models
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration

_VECTOR = [1.0] + [0.0] * 1535
_SOURCE_QUESTION = "How long does a refund take?"
_SOURCE_REPLY = "Refunds take 3 to 5 business days."
_JA_QUERY = "返金はいつ反映されますか？"
_JA_REPLY = "返金には通常3から5営業日かかります。"

_SENT_TEXTS: list[str] = []


class _FakeTelegramSender:
    async def send_text(self, *, target, text):
        _SENT_TEXTS.append(text)
        return f"sent-{len(_SENT_TEXTS)}"

    async def aclose(self):
        return None


class _CrossLingualTestEmbedder:
    version = "test-cross-lingual-v1"

    async def embed(self, texts):
        return [list(_VECTOR) for _text in texts]



class _RuntimeLLM:
    def __init__(
        self,
        replies,
        *,
        faithful=True,
        translated_query=None,
        detected_language=None,
    ):
        self.replies = replies
        self.faithful = faithful
        self.grounding_verifier_id = "runtime-grounding-test"
        self.translated_query = translated_query
        self.detected_language = detected_language

    async def decide(self, context):
        return ReplyDecision(
            action=ReplyAction.AUTO_REPLY,
            reply_text=self.replies[context.target_language],
            intent="refund_timing",
            confidence=0.99,
        )

    async def verify_grounding(self, **kwargs):
        return self.faithful
    async def translate_to_english(self, text):
        return self.translated_query

    async def detect_language_tag(self, text):
        return self.detected_language

class _RaisingLLM:
    async def decide(self, context):
        raise RuntimeError("llm unavailable")



@pytest.fixture
async def multilingual_runtime(monkeypatch):
    monkeypatch.setenv("CHATWOOT_ENABLED", "false")
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "true")
    monkeypatch.setenv("KNOWLEDGE_VERBATIM_REPLY", "false")
    monkeypatch.setenv("MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED", "true")
    monkeypatch.setenv("ENGLISH_KNOWLEDGE_ONLY_ENABLED", "true")
    monkeypatch.setenv("KNOWLEDGE_AUTO_REPLY_MIN_SIMILARITY", "0.8")
    monkeypatch.setenv("KNOWLEDGE_AUTO_REPLY_MIN_MARGIN", "0.08")
    get_settings.cache_clear()
    _SENT_TEXTS.clear()
    registry._senders.clear()
    runner._embedder = _CrossLingualTestEmbedder()
    runner._llm = None

    async def fake_get_platform_sender(account_id):
        return _FakeTelegramSender()

    monkeypatch.setattr(outbox_module, "get_platform_sender", fake_get_platform_sender)
    try:
        yield monkeypatch
    finally:
        runner._embedder = None
        registry._senders.clear()
        runner._llm = None
        get_settings.cache_clear()


async def _seed_conversation(session, *, text: str, tenant_id: str = "default"):
    account_id, contact_id, conversation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id=tenant_id,
            brand_id="b1",
            platform="telegram",
            name="localization-test",
            config={"delivery_mode": "direct"},
            capability={"dm": True, "max_text_length": 4096},
            chatwoot_inbox_id=101,
        )
    )
    registry._senders[("telegram", account_id, 1, 0)] = _FakeTelegramSender()
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id=tenant_id,
            platform="telegram",
            platform_account_id=account_id,
            external_user_id=str(uuid.uuid4()),
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id=tenant_id,
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"telegram:test:{account_id}",
        )
    )
    message_id = uuid.uuid4()
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text=text,
            chatwoot_message_id=55,
            reply_target={"chat_id": "localization-user"},
        )
    )
    await ensure_state(session, conversation_id, "BOT_ACTIVE")
    await session.commit()
    return account_id, conversation_id, message_id


def _snapshot(account_id: uuid.UUID, text: str, *, tenant_id: str = "default"):
    return DecisionSnapshot(
        text=text,
        platform="telegram",
        tenant_id=tenant_id,
        brand_id="b1",
        account_id=str(account_id),
        conversation_key=f"telegram:test:{account_id}",
        automation_state="BOT_ACTIVE",
        state_version=1,
    )


async def _seed_english_policy(
    session,
    *,
    tenant_id: str = "default",
    brand_id: str = "b1",
    platform: str | None = None,
    question: str = _SOURCE_QUESTION,
    reply: str = _SOURCE_REPLY,
    is_official_contact: bool = False,
):
    document_id = uuid.uuid4()
    content = f"Question: {question}\nApproved answer: {reply}"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    await session.execute(
        insert(models.KnowledgeDocument).values(
            id=document_id,
            tenant_id=tenant_id,
            brand_id=brand_id,
            platform=platform,
            question=question,
            reply=reply,
            status="published",
            source_language="en",
            detected_language="en",
            language_detection_status="english",
            language_verified=True,
            is_official_contact=is_official_contact,
        )
    )
    await session.execute(
        insert(models.KnowledgeChunk).values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            document_id=document_id,
            content=content,
            embed_text=question,
            content_hash=content_hash,
            embedding_version=_CrossLingualTestEmbedder.version,
            embedding=_VECTOR,
        )
    )
    await session.commit()
    return document_id, content_hash


async def _publish_ja_localization(
    session,
    *,
    document_id: uuid.UUID,
    tenant_id="default",
    release_id="test-ja-v1",
    text=_JA_REPLY,
):
    artifact = await create_localization_draft(
        session,
        LocalizationDraftInput(
            tenant_id=tenant_id,
            document_id=document_id,
            release_id=release_id,
            locale="ja",
            text=text,
            source_file="tests/fixtures/knowledge-localizations-ja.csv",
        ),
    )
    await publish_localization(
        session,
        tenant_id=tenant_id,
        artifact_id=artifact.id,
        reviewer="reviewer@example.test",
        approve_auto_reply=True,
        approve_official_contact=False,
    )
    await session.commit()
    return artifact


async def _run(session, *, text: str, tenant_id: str = "default"):
    account_id, conversation_id, message_id = await _seed_conversation(
        session, text=text, tenant_id=tenant_id
    )
    outbox_id = await runner.run_and_persist_decision(
        _snapshot(account_id, text, tenant_id=tenant_id),
        conversation_id,
        message_id,
        account_id,
    )
    return conversation_id, outbox_id


async def _assert_handoff(session, conversation_id: uuid.UUID, reason_code: str):
    state = await session.scalar(
        select(models.AutomationState).where(
            models.AutomationState.conversation_id == conversation_id
        )
    )
    assert state.state == "HANDOFF_PENDING"
    work_items = (
        (
            await session.execute(
                select(models.HumanWorkItem).where(
                    models.HumanWorkItem.conversation_id == conversation_id,
                    models.HumanWorkItem.status.in_(["WAITING", "CLAIMED"]),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(work_items) == 1
    assert work_items[0].reason_code == reason_code


@pytest.mark.parametrize(
    ("query", "language", "reply"),
    [
        (_JA_QUERY, "ja", _JA_REPLY),
        (
            "환불은 언제 처리되나요?",
            "ko",
            "환불은 보통 영업일 기준 3~5일이 걸립니다.",
        ),
        (
            "रिफंड मिलने में कितना समय लगता है?",
            "hi",
            "रिफंड में आमतौर पर 3 से 5 कार्यदिवस लगते हैं।",
        ),
        (
            "Berapa lama pengembalian dana diproses?",
            "id",
            (
                "Menurut kebijakan pembayaran, pengembalian dana akan diproses dalam 3 hingga "
                "5 hari kerja."
            ),
        ),
    ],
)
async def test_runtime_generation_supports_detected_languages(
    session, multilingual_runtime, query, language, reply
):
    await _seed_english_policy(session)
    runner._llm = _RuntimeLLM({language: reply})

    conversation_id, outbox_id = await _run(session, text=query)
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()

    assert outbox_id is not None, (
        language,
        decision.reason_codes,
        decision.request_language,
        decision.reply_language,
    )

    assert decision.action == "auto_reply"
    assert decision.request_language.split("-", 1)[0] == language
    assert decision.reply_language.split("-", 1)[0] == language
    assert decision.resolved_locale.split("-", 1)[0] == language
    assert decision.multilingual_contract_version == "multilingual-runtime-generation-v1"
    assert decision.grounding_verified is True
    assert "MULTILINGUAL_RUNTIME_GENERATION" in decision.reason_codes
    outbox = await session.get(models.OutboxMessage, outbox_id)
    assert outbox.status == "SENT"
    assert outbox.payload["text"] == reply
    assert _SENT_TEXTS == [reply]
    assert conversation_id is not None


async def test_english_request_keeps_canonical_verbatim_path(session, multilingual_runtime):
    question = "Could you please explain how long a refund usually takes in business days?"
    await _seed_english_policy(session, question=question)
    runner._llm = _RuntimeLLM({"en": _SOURCE_REPLY})

    _conversation_id, outbox_id = await _run(session, text=question)

    assert outbox_id is not None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "auto_reply"
    assert decision.reply_text == _SOURCE_REPLY
    assert decision.reply_language == "en"
    assert decision.multilingual_contract_version == "multilingual-runtime-generation-v1"
    assert decision.grounding_verified is True
    assert "MULTILINGUAL_RUNTIME_GENERATION" in decision.reason_codes


async def test_unknown_language_handoffs(session, multilingual_runtime):
    await _seed_english_policy(session)
    conversation_id, outbox_id = await _run(session, text="@@@ 123456")

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert "UNKNOWN_LANGUAGE" in decision.reason_codes
    await _assert_handoff(session, conversation_id, "UNKNOWN_LANGUAGE")


async def test_wrong_language_reply_handoffs_before_outbox(session, multilingual_runtime):
    await _seed_english_policy(session)
    runner._llm = _RuntimeLLM({"ja": _SOURCE_REPLY})
    conversation_id, outbox_id = await _run(session, text=_JA_QUERY)

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert "GUARD_LANGUAGE_MISMATCH" in decision.reason_codes
    await _assert_handoff(session, conversation_id, "GUARD_LANGUAGE_MISMATCH")


async def test_grounding_failure_handoffs(session, multilingual_runtime):
    await _seed_english_policy(session)
    runner._llm = _RuntimeLLM({"ja": _JA_REPLY}, faithful=False)
    conversation_id, outbox_id = await _run(session, text=_JA_QUERY)

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert "GUARD_KNOWLEDGE_SEMANTIC_MISMATCH" in decision.reason_codes
    await _assert_handoff(session, conversation_id, "GUARD_KNOWLEDGE_SEMANTIC_MISMATCH")


async def test_llm_failure_handoffs(session, multilingual_runtime):
    await _seed_english_policy(session)
    runner._llm = _RaisingLLM()
    conversation_id, outbox_id = await _run(session, text=_JA_QUERY)

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert "MULTILINGUAL_GENERATION_FAILED" in decision.reason_codes
    assert decision.multilingual_contract_version == "multilingual-runtime-generation-v1"
    await _assert_handoff(session, conversation_id, "MULTILINGUAL_GENERATION_FAILED")


async def test_official_contact_never_uses_runtime_generation(session, multilingual_runtime):
    await _seed_english_policy(session, is_official_contact=True)
    conversation_id, outbox_id = await _run(session, text=_JA_QUERY)

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert "MULTILINGUAL_OFFICIAL_CONTACT_REVIEW" in decision.reason_codes
    await _assert_handoff(session, conversation_id, "MULTILINGUAL_OFFICIAL_CONTACT_REVIEW")


async def test_english_official_contact_handoffs_before_generation(session, multilingual_runtime):
    question = "Could you please explain how long a refund usually takes in business days?"
    await _seed_english_policy(session, question=question, is_official_contact=True)
    runner._llm = _RaisingLLM()

    conversation_id, outbox_id = await _run(session, text=question)

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert "MULTILINGUAL_OFFICIAL_CONTACT_REVIEW" in decision.reason_codes
    assert decision.multilingual_contract_version is None
    await _assert_handoff(session, conversation_id, "MULTILINGUAL_OFFICIAL_CONTACT_REVIEW")


async def test_query_translation_failure_keeps_original_handoff(
    session, multilingual_runtime, monkeypatch
):
    await _seed_english_policy(session)
    runner._llm = _RuntimeLLM({"ja": _JA_REPLY}, translated_query="__QTP_0__")

    async def weak_fetch(snapshot, *, query_text=None, **kwargs):
        return KnowledgeRetrievalResult()

    monkeypatch.setattr(runner, "_fetch_knowledge", weak_fetch)
    conversation_id, outbox_id = await _run(session, text="メールは test@example.com ですか？")

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert "NO_STRONG_KNOWLEDGE_MATCH" in decision.reason_codes
    await _assert_handoff(session, conversation_id, "NO_STRONG_KNOWLEDGE_MATCH")


async def test_bot_draft_private_note_survives_runtime_preflight(
    session, multilingual_runtime, monkeypatch
):
    multilingual_runtime.setenv("CHATWOOT_ENABLED", "true")
    get_settings.cache_clear()
    await _seed_english_policy(session)
    account_id, conversation_id, message_id = await _seed_conversation(
        session, text=_JA_QUERY
    )
    await session.execute(
        update(models.PlatformAccount)
        .where(models.PlatformAccount.id == account_id)
        .values(config={"delivery_mode": "chatwoot"})
    )
    await session.commit()
    runner._llm = _RuntimeLLM({"ja": _JA_REPLY})
    snapshot = _snapshot(account_id, _JA_QUERY)
    snapshot = snapshot.__class__(
        **{**snapshot.__dict__, "automation_state": "BOT_DRAFT_ONLY"}
    )

    outbox_id = await runner.run_and_persist_decision(
        snapshot, conversation_id, message_id, account_id
    )

    assert outbox_id is not None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "draft"
    outbox = await session.get(models.OutboxMessage, outbox_id)
    assert outbox.message_type == "private_note"
    result = await outbox_module._localization_send_preflight(
        session,
        outbox_id=outbox_id,
        platform_account_id=account_id,
        payload_text=outbox.payload["text"],
        message_type=outbox.message_type,
    )
    assert result is None


# 尼泊尔语：detect_language 主动 fail-closed（_NEPALI_HINTS），确定性检测判不出。
# 这条链路验证 llm_fallback → lenient 输出闸门 → 投递闸门 的完整自洽性。
_NE_QUERY = "म पैसा फिर्ता कहिले पाउँछु?"
_NE_REPLY = "फिर्ता सामान्यतया 3 देखि 5 कार्य दिन लाग्छ।"


async def test_llm_resolved_language_passes_lenient_guard_and_outbox(
    session, multilingual_runtime
):
    await _seed_english_policy(session)
    runner._llm = _RuntimeLLM({"ne": _NE_REPLY}, detected_language="ne")

    conversation_id, outbox_id = await _run(session, text=_NE_QUERY)
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()

    assert outbox_id is not None, (decision.reason_codes, decision.request_language)
    assert decision.action == "auto_reply"
    assert decision.request_language_source == "llm_fallback"
    assert decision.request_language == "ne"
    # 投递闸门对 und 是硬拒绝，lenient 模式必须落成真实语言标签。
    assert decision.reply_language == "ne"
    assert decision.resolved_locale == "ne"
    assert "LANGUAGE_MODEL_ATTESTED" in decision.reason_codes
    assert decision.grounding_verified is True

    outbox = await session.get(models.OutboxMessage, outbox_id)
    assert outbox.status == "SENT"
    assert outbox.payload["text"] == _NE_REPLY
    assert _SENT_TEXTS == [_NE_REPLY]

    # 投递前复核也必须放行，确认 guard 与 outbox 两侧对 lenient 的语义一致。
    assert (
        await outbox_module._localization_send_preflight(
            session,
            outbox_id=outbox_id,
            platform_account_id=(
                await session.scalar(
                    select(models.Conversation.platform_account_id).where(
                        models.Conversation.id == conversation_id
                    )
                )
            ),
            payload_text=outbox.payload["text"],
            message_type=outbox.message_type,
        )
        is None
    )


async def test_llm_language_fallback_unavailable_keeps_unknown_language_handoff(
    session, multilingual_runtime
):
    # 兜底能力不可用时必须退回原有的 fail-closed 行为，而不是猜一个语言。
    await _seed_english_policy(session)
    runner._llm = _RuntimeLLM({"ne": _NE_REPLY}, detected_language=None)

    conversation_id, outbox_id = await _run(session, text=_NE_QUERY)

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert "UNKNOWN_LANGUAGE" in decision.reason_codes
    await _assert_handoff(session, conversation_id, "UNKNOWN_LANGUAGE")
