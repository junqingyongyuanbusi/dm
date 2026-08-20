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
):
    document_id = uuid.uuid4()
    content = f"Question: {_SOURCE_QUESTION}\nApproved answer: {_SOURCE_REPLY}"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    await session.execute(
        insert(models.KnowledgeDocument).values(
            id=document_id,
            tenant_id=tenant_id,
            brand_id=brand_id,
            platform=platform,
            question=_SOURCE_QUESTION,
            reply=_SOURCE_REPLY,
            status="published",
            source_language="en",
            detected_language="en",
            language_detection_status="english",
            language_verified=True,
            is_official_contact=False,
        )
    )
    await session.execute(
        insert(models.KnowledgeChunk).values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            document_id=document_id,
            content=content,
            embed_text=_SOURCE_QUESTION,
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
