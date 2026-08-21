import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select, update

import social_reply.infrastructure.queue.broker  # noqa: F401
from social_reply.application.knowledge.localizations import (
    LocalizationDraftInput,
    LocalizationValidationError,
    create_localization_draft,
    publish_localization,
    revoke_localization,
)
from social_reply.application.message_delivery import outbox as outbox_module
from social_reply.application.message_delivery.outbox import deliver_outbox
from social_reply.application.reply_decision import runner
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.connectors import registry
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.domain.reply.language import detect_language
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


class _NeverLLM:
    async def decide(self, context):
        raise AssertionError("reviewed localization path must not call the LLM")


class _ExperimentalLLM:
    def __init__(self, replies, *, faithful=True):
        self.replies = replies
        self.faithful = faithful
        self.grounding_verifier_id = "experimental-grounding-test"

    async def decide(self, context):
        return ReplyDecision(
            action=ReplyAction.AUTO_REPLY,
            reply_text=self.replies[context.target_language],
            intent="refund_timing",
            confidence=0.99,
        )

    async def verify_grounding(self, **kwargs):
        return self.faithful


class _RaisingExperimentalLLM:
    async def decide(self, context):
        raise RuntimeError("llm unavailable")


@pytest.fixture
async def experimental_runtime(monkeypatch):
    monkeypatch.setenv("CHATWOOT_ENABLED", "false")
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "true")
    monkeypatch.setenv("KNOWLEDGE_VERBATIM_REPLY", "true")
    monkeypatch.setenv("MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED", "false")
    monkeypatch.setenv("MULTILINGUAL_KNOWLEDGE_SHADOW_ENABLED", "false")
    monkeypatch.setenv("ENGLISH_KNOWLEDGE_ONLY_ENABLED", "false")
    monkeypatch.setenv("MULTILINGUAL_EXPERIMENTAL_REPLY_ENABLED", "false")
    monkeypatch.setenv("MULTILINGUAL_EXPERIMENTAL_ACCOUNT_IDS", "")
    monkeypatch.setenv("MULTILINGUAL_EXPERIMENTAL_MIN_SIMILARITY", "0.5")
    monkeypatch.setenv("MULTILINGUAL_EXPERIMENTAL_MIN_MARGIN", "0.08")
    monkeypatch.setenv("KNOWLEDGE_AUTO_REPLY_MIN_SIMILARITY", "0.8")
    monkeypatch.setenv("KNOWLEDGE_AUTO_REPLY_MIN_MARGIN", "0.08")
    _SENT_TEXTS.clear()
    registry._senders.clear()

    async def fake_get_platform_sender(account_id):
        return _FakeTelegramSender()

    monkeypatch.setattr(outbox_module, "get_platform_sender", fake_get_platform_sender)

    def enable(account_id: uuid.UUID, llm) -> None:
        monkeypatch.setenv("MULTILINGUAL_EXPERIMENTAL_REPLY_ENABLED", "true")
        monkeypatch.setenv("MULTILINGUAL_EXPERIMENTAL_ACCOUNT_IDS", str(account_id))
        runner._embedder = _CrossLingualTestEmbedder()
        runner._llm = llm
        get_settings.cache_clear()

    try:
        yield enable
    finally:
        runner._embedder = None
        runner._llm = None
        registry._senders.clear()
        get_settings.cache_clear()


@pytest.fixture
async def multilingual_runtime(monkeypatch):
    monkeypatch.setenv("CHATWOOT_ENABLED", "false")
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "true")
    monkeypatch.setenv("KNOWLEDGE_VERBATIM_REPLY", "true")
    monkeypatch.setenv("MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED", "true")
    monkeypatch.setenv("ENGLISH_KNOWLEDGE_ONLY_ENABLED", "true")
    monkeypatch.setenv("MULTILINGUAL_LIVE_LOCALES", "ja")
    monkeypatch.setenv("KNOWLEDGE_LOCALIZATION_RELEASE", "test-ja-v1")
    monkeypatch.setenv("KNOWLEDGE_AUTO_REPLY_MIN_SIMILARITY", "0.8")
    monkeypatch.setenv("KNOWLEDGE_AUTO_REPLY_MIN_MARGIN", "0.08")
    get_settings.cache_clear()
    _SENT_TEXTS.clear()
    registry._senders.clear()
    runner._embedder = _CrossLingualTestEmbedder()
    runner._llm = _NeverLLM()

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


async def test_japanese_inbox_uses_published_localization_and_creates_outbox(
    session, multilingual_runtime
):
    document_id, content_hash = await _seed_english_policy(session)
    artifact = await _publish_ja_localization(session, document_id=document_id)

    _conversation_id, outbox_id = await _run(session, text=_JA_QUERY)

    assert outbox_id is not None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "auto_reply"
    assert decision.request_language == "ja"
    assert decision.request_language_confidence == 1.0
    assert decision.resolved_locale == "ja"
    assert decision.reply_language == "ja"
    assert decision.reply_text == _JA_REPLY
    assert decision.knowledge_content_hash == content_hash
    assert decision.knowledge_localization_id == artifact.id
    assert decision.knowledge_localization_release_id == "test-ja-v1"
    assert decision.knowledge_localization_text_hash == artifact.text_hash
    assert decision.multilingual_contract_version == "multilingual-v2-reviewed-localization"
    outbox = await session.get(models.OutboxMessage, outbox_id)
    assert outbox.status == "SENT", (
        outbox.last_error_code,
        outbox.last_error_message,
        outbox.destination_type,
        outbox.payload,
    )
    assert _SENT_TEXTS == [_JA_REPLY]
    assert outbox.payload["text"] == _JA_REPLY
    assert await deliver_outbox(str(outbox_id)) == "SKIPPED_NOT_CLAIMABLE"
    assert _SENT_TEXTS == [_JA_REPLY]


async def test_pinned_release_ignores_newer_unpromoted_release(session, multilingual_runtime):
    document_id, _content_hash = await _seed_english_policy(session)
    approved = await _publish_ja_localization(session, document_id=document_id)
    await _publish_ja_localization(
        session,
        document_id=document_id,
        release_id="test-ja-v2",
        text="返金は通常3から5営業日で処理されます。",
    )

    _conversation_id, outbox_id = await _run(session, text=_JA_QUERY)

    assert outbox_id is not None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.knowledge_localization_id == approved.id
    assert decision.knowledge_localization_release_id == "test-ja-v1"
    assert decision.reply_text == _JA_REPLY


async def test_english_canonical_reply_remains_available_during_ja_rollout(
    session, multilingual_runtime
):
    await _seed_english_policy(session)

    _conversation_id, outbox_id = await _run(session, text=_SOURCE_QUESTION)

    assert outbox_id is not None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "auto_reply"
    assert decision.request_language == "en"
    assert decision.resolved_locale == "en"
    assert decision.reply_language == "en"
    assert decision.reply_text == _SOURCE_REPLY
    assert decision.knowledge_localization_id is None


async def test_english_live_respects_verbatim_disable(session, multilingual_runtime):
    multilingual_runtime.setenv("KNOWLEDGE_VERBATIM_REPLY", "false")
    get_settings.cache_clear()
    await _seed_english_policy(session)

    conversation_id, outbox_id = await _run(session, text=_SOURCE_QUESTION)

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert "ENGLISH_CANONICAL_VERBATIM_DISABLED" in decision.reason_codes
    await _assert_handoff(session, conversation_id, "ENGLISH_CANONICAL_VERBATIM_DISABLED")


@pytest.mark.parametrize(
    ("text", "expected_reason"),
    [
        (_JA_QUERY, "NO_APPROVED_LOCALIZATION"),
        ("返金希望", "UNKNOWN_LANGUAGE"),
    ],
)
async def test_missing_localization_or_ambiguous_language_handoffs(
    session, multilingual_runtime, text, expected_reason
):
    await _seed_english_policy(session)

    conversation_id, outbox_id = await _run(session, text=text)

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert expected_reason in decision.reason_codes
    assert (await session.execute(select(models.OutboxMessage))).scalars().all() == []
    await _assert_handoff(session, conversation_id, expected_reason)


async def test_wrong_scope_policy_does_not_authorize_japanese_reply(session, multilingual_runtime):
    document_id, _content_hash = await _seed_english_policy(session, brand_id="other")
    await _publish_ja_localization(session, document_id=document_id)

    conversation_id, outbox_id = await _run(session, text=_JA_QUERY)

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert "NO_STRONG_KNOWLEDGE_MATCH" in decision.reason_codes
    await _assert_handoff(session, conversation_id, "NO_STRONG_KNOWLEDGE_MATCH")


async def test_wrong_language_localization_is_blocked_by_final_guard(session, multilingual_runtime):
    document_id, content_hash = await _seed_english_policy(session)
    bad_text = "Refunds take 3 to 5 business days."
    artifact_id = uuid.uuid4()
    await session.execute(
        insert(models.KnowledgeLocalization).values(
            id=artifact_id,
            tenant_id="default",
            document_id=document_id,
            release_id="test-ja-v1",
            locale="ja",
            localized_text=bad_text,
            text_hash=hashlib.sha256(bad_text.encode()).hexdigest(),
            source_content_hash=content_hash,
            protected_values=[],
            auto_reply_allowed=True,
            official_contact_authorized=False,
            status="published",
            reviewed_by="reviewer@example.test",
            reviewed_at=datetime(2026, 8, 19, tzinfo=UTC),
        )
    )
    await session.commit()

    conversation_id, outbox_id = await _run(session, text=_JA_QUERY)

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert "GUARD_LANGUAGE_MISMATCH" in decision.reason_codes
    await _assert_handoff(session, conversation_id, "GUARD_LANGUAGE_MISMATCH")


async def test_revoked_after_decision_is_cancelled_and_handed_off(session, multilingual_runtime):
    async def defer_fast_path(outbox_id):
        return "DEFERRED_TEST"

    multilingual_runtime.setattr(outbox_module, "deliver_outbox", defer_fast_path)
    document_id, _content_hash = await _seed_english_policy(session)
    artifact = await _publish_ja_localization(session, document_id=document_id)
    conversation_id, outbox_id = await _run(session, text=_JA_QUERY)
    assert outbox_id is not None
    pending = await session.get(models.OutboxMessage, outbox_id)
    assert pending.status == "PENDING"
    assert _SENT_TEXTS == []

    await revoke_localization(
        session,
        tenant_id="default",
        artifact_id=artifact.id,
        actor="reviewer@example.test",
        reason="policy review rollback",
    )
    await session.commit()

    result = await deliver_outbox(str(outbox_id))

    assert result == "CANCELLED"
    await session.rollback()
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, outbox_id)
    assert outbox.status == "CANCELLED"
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
    assert work_items[0].reason_code == "LOCALIZATION_RELEASE_REVOKED"
    notifications = (
        (
            await session.execute(
                select(models.HandoffNotificationIntent).where(
                    models.HandoffNotificationIntent.conversation_id == conversation_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(notifications) == 1


async def test_revoke_refuses_artifact_with_sending_outbox(session, multilingual_runtime):
    async def defer_fast_path(outbox_id):
        return "DEFERRED_TEST"

    multilingual_runtime.setattr(outbox_module, "deliver_outbox", defer_fast_path)
    document_id, _content_hash = await _seed_english_policy(session)
    artifact = await _publish_ja_localization(session, document_id=document_id)
    _conversation_id, outbox_id = await _run(session, text=_JA_QUERY)
    assert outbox_id is not None
    await session.execute(
        update(models.OutboxMessage)
        .where(models.OutboxMessage.id == outbox_id)
        .values(status="SENDING")
    )
    await session.commit()

    with pytest.raises(LocalizationValidationError, match="sending outbox"):
        await revoke_localization(
            session,
            tenant_id="default",
            artifact_id=artifact.id,
            actor="reviewer@example.test",
            reason="concurrent rollback",
        )


@pytest.mark.parametrize(
    ("env_key", "env_value", "expected_reason"),
    [
        ("MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED", "false", "MULTILINGUAL_LIVE_DISABLED"),
        ("MULTILINGUAL_LIVE_LOCALES", "fr", "MULTILINGUAL_LOCALE_DISABLED"),
    ],
)
async def test_delivery_rechecks_live_switch_and_locale(
    session, multilingual_runtime, env_key, env_value, expected_reason
):
    async def defer_fast_path(outbox_id):
        return "DEFERRED_TEST"

    multilingual_runtime.setattr(outbox_module, "deliver_outbox", defer_fast_path)
    document_id, _content_hash = await _seed_english_policy(session)
    await _publish_ja_localization(session, document_id=document_id)
    conversation_id, outbox_id = await _run(session, text=_JA_QUERY)
    assert outbox_id is not None

    multilingual_runtime.setenv(env_key, env_value)
    get_settings.cache_clear()
    result = await deliver_outbox(str(outbox_id))

    assert result == "CANCELLED"
    await session.rollback()
    session.expire_all()
    await _assert_handoff(session, conversation_id, expected_reason)


@pytest.mark.parametrize(
    ("query", "language", "reply"),
    [
        (_JA_QUERY, "ja", _JA_REPLY),
        ("退款需要多长时间？", "zh-Hans", "退款通常需要3到5个工作日。"),
        (
            "¿Cuánto tarda un reembolso?",
            "es",
            "Los reembolsos tardan de 3 a 5 días laborables.",
        ),
        (
            "Combien de temps prend un remboursement ?",
            "fr",
            "Les remboursements prennent généralement de 3 à 5 jours ouvrables.",
        ),
    ],
)
async def test_allowlisted_experimental_account_sends_same_language(
    session, experimental_runtime, query, language, reply
):
    await _seed_english_policy(session)
    account_id, conversation_id, message_id = await _seed_conversation(session, text=query)
    experimental_runtime(account_id, _ExperimentalLLM({language: reply}))

    outbox_id = await runner.run_and_persist_decision(
        _snapshot(account_id, query), conversation_id, message_id, account_id
    )

    assert outbox_id is not None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "auto_reply"
    assert decision.request_language == language
    assert decision.reply_language == language
    assert decision.resolved_locale == language
    assert decision.multilingual_contract_version == "multilingual-experimental-runtime-v1"
    assert "EXPERIMENTAL_UNVERIFIED_CORPUS" in decision.reason_codes
    assert "KNOWLEDGE_VERBATIM" not in decision.reason_codes
    assert decision.grounding_verified is True
    assert decision.knowledge_min_similarity_threshold == 0.5
    assert decision.knowledge_min_margin_threshold == 0.08
    outbox = await session.get(models.OutboxMessage, outbox_id)
    assert outbox.status == "SENT"
    assert outbox.payload["text"] == reply
    assert _SENT_TEXTS == [reply]


async def test_experimental_accepts_english_answer_when_question_lid_is_unknown(
    session, experimental_runtime
):
    question = "How can I check a broker’s license on WikiFX?"
    answer = "Open the broker profile on WikiFX and review the Regulatory Information section."
    query = "WikiFXでブローカーのライセンスを確認するにはどうすればいいですか？"
    reply = "WikiFXでブローカーのプロフィールを開き、規制情報セクションを確認してください。"
    assert detect_language(question).tag == "und"
    assert detect_language(answer).tag == "en"
    await _seed_english_policy(session, question=question, reply=answer)
    account_id, conversation_id, message_id = await _seed_conversation(session, text=query)
    experimental_runtime(account_id, _ExperimentalLLM({"ja": reply}))

    outbox_id = await runner.run_and_persist_decision(
        _snapshot(account_id, query), conversation_id, message_id, account_id
    )

    assert outbox_id is not None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "auto_reply"
    assert decision.request_language == "ja"
    assert decision.reply_language == "ja"
    assert "EXPERIMENTAL_KNOWLEDGE_NOT_ENGLISH" not in decision.reason_codes


async def test_non_allowlisted_account_keeps_legacy_verbatim(session, experimental_runtime):
    await _seed_english_policy(session)
    account_id, conversation_id, message_id = await _seed_conversation(session, text=_JA_QUERY)
    experimental_runtime(uuid.uuid4(), _ExperimentalLLM({"ja": _JA_REPLY}))

    outbox_id = await runner.run_and_persist_decision(
        _snapshot(account_id, _JA_QUERY), conversation_id, message_id, account_id
    )

    assert outbox_id is not None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.reply_text == _SOURCE_REPLY
    assert "KNOWLEDGE_VERBATIM" in decision.reason_codes
    assert decision.multilingual_contract_version is None


async def test_allowlisted_account_english_canonical_remains_legacy(session, experimental_runtime):
    await _seed_english_policy(session)
    account_id, conversation_id, message_id = await _seed_conversation(
        session, text=_SOURCE_QUESTION
    )
    experimental_runtime(account_id, _ExperimentalLLM({"en": _SOURCE_REPLY}))

    outbox_id = await runner.run_and_persist_decision(
        _snapshot(account_id, _SOURCE_QUESTION), conversation_id, message_id, account_id
    )

    assert outbox_id is not None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.reply_text == _SOURCE_REPLY
    assert decision.multilingual_contract_version is None
    assert "KNOWLEDGE_VERBATIM" in decision.reason_codes


@pytest.mark.parametrize(
    ("llm", "expected_reason"),
    [
        (_ExperimentalLLM({"ja": _SOURCE_REPLY}), "GUARD_LANGUAGE_MISMATCH"),
        (_ExperimentalLLM({"ja": _JA_REPLY}, faithful=False), "GUARD_KNOWLEDGE_SEMANTIC_MISMATCH"),
        (_RaisingExperimentalLLM(), "EXPERIMENTAL_LLM_FAILED"),
    ],
)
async def test_experimental_wrong_language_or_grounding_failure_handoffs(
    session, experimental_runtime, llm, expected_reason
):
    await _seed_english_policy(session)
    account_id, conversation_id, message_id = await _seed_conversation(session, text=_JA_QUERY)
    experimental_runtime(account_id, llm)

    outbox_id = await runner.run_and_persist_decision(
        _snapshot(account_id, _JA_QUERY), conversation_id, message_id, account_id
    )

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert expected_reason in decision.reason_codes
    assert _SENT_TEXTS == []
    await _assert_handoff(session, conversation_id, expected_reason)


@pytest.mark.parametrize(
    ("env_key", "env_value", "expected_reason"),
    [
        (
            "MULTILINGUAL_EXPERIMENTAL_REPLY_ENABLED",
            "false",
            "EXPERIMENTAL_MULTILINGUAL_DISABLED",
        ),
        (
            "MULTILINGUAL_EXPERIMENTAL_ACCOUNT_IDS",
            "12345678-1234-5678-1234-567812345678",
            "EXPERIMENTAL_MULTILINGUAL_ACCOUNT_DISABLED",
        ),
    ],
)
async def test_experimental_disable_before_send_cancels_and_handoffs(
    session, experimental_runtime, monkeypatch, env_key, env_value, expected_reason
):
    async def defer_fast_path(outbox_id):
        return "DEFERRED_TEST"

    monkeypatch.setattr(outbox_module, "deliver_outbox", defer_fast_path)
    await _seed_english_policy(session)
    account_id, conversation_id, message_id = await _seed_conversation(session, text=_JA_QUERY)
    experimental_runtime(account_id, _ExperimentalLLM({"ja": _JA_REPLY}))
    outbox_id = await runner.run_and_persist_decision(
        _snapshot(account_id, _JA_QUERY), conversation_id, message_id, account_id
    )
    assert outbox_id is not None

    monkeypatch.setenv(env_key, env_value)
    get_settings.cache_clear()
    result = await deliver_outbox(str(outbox_id))

    assert result == "CANCELLED"
    assert _SENT_TEXTS == []
    await session.rollback()
    session.expire_all()
    await _assert_handoff(session, conversation_id, expected_reason)


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("ambiguous", "UNKNOWN_LANGUAGE"),
        ("low_margin", "NO_STRONG_KNOWLEDGE_MATCH"),
        ("official_contact", "MULTILINGUAL_OFFICIAL_CONTACT_REVIEW"),
        ("provider_failure", "KNOWLEDGE_RETRIEVAL_FAILED"),
        ("non_english_source", "EXPERIMENTAL_KNOWLEDGE_NOT_ENGLISH"),
        ("unknown_reply", "EXPERIMENTAL_KNOWLEDGE_NOT_ENGLISH"),
    ],
)
async def test_experimental_fail_closed_boundaries(
    session, experimental_runtime, case, expected_reason
):
    if case == "official_contact":
        await _seed_english_policy(session, is_official_contact=True)
        query = _JA_QUERY
    elif case == "non_english_source":
        await _seed_english_policy(
            session,
            question="退款需要多长时间？",
            reply="退款通常需要3到5个工作日。",
        )
        query = _JA_QUERY
    elif case == "unknown_reply":
        source_question = "How can I check a broker license?"
        source_reply = "N/A"
        assert detect_language(source_question).tag == "en"
        assert detect_language(source_reply).tag == "und"
        await _seed_english_policy(
            session,
            question=source_question,
            reply=source_reply,
        )
        query = _JA_QUERY
    else:
        await _seed_english_policy(session)
        query = "返金希望" if case == "ambiguous" else _JA_QUERY
    if case == "low_margin":
        await _seed_english_policy(
            session,
            question="How long does account verification take?",
            reply="Verification takes 7 business days.",
        )

    account_id, conversation_id, message_id = await _seed_conversation(session, text=query)
    experimental_runtime(account_id, _ExperimentalLLM({"ja": _JA_REPLY}))
    if case == "provider_failure":

        class FailingEmbedder:
            version = _CrossLingualTestEmbedder.version

            async def embed(self, texts):
                raise RuntimeError("provider unavailable")

        runner._embedder = FailingEmbedder()

    outbox_id = await runner.run_and_persist_decision(
        _snapshot(account_id, query), conversation_id, message_id, account_id
    )

    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "handoff"
    assert expected_reason in decision.reason_codes
    assert _SENT_TEXTS == []
    await _assert_handoff(session, conversation_id, expected_reason)
