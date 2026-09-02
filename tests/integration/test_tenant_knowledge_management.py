import asyncio
import hashlib
import uuid

import httpx
import pytest
from sqlalchemy import select

from apps.api.main import create_app
from social_reply.application.account_management.auth import hash_password
from social_reply.application.knowledge.commands import (
    ConfirmKnowledgeEnglishCommand,
    CreateKnowledgeDocumentCommand,
    DeleteKnowledgeDraftCommand,
    ImportKnowledgeBatchCommand,
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    KnowledgeValidationError,
    SetKnowledgeOfficialContactCommand,
    execute_confirm_knowledge_english,
    execute_create_knowledge_document,
    execute_delete_knowledge_draft,
    execute_import_knowledge_batch,
    execute_set_knowledge_official_contact,
)
from social_reply.application.knowledge.publication import (
    BulkPublishKnowledgeCommand,
    PublishKnowledgeCommand,
    UnpublishKnowledgeCommand,
    execute_bulk_publish_knowledge,
    execute_publish_knowledge,
    execute_unpublish_knowledge,
)
from social_reply.application.knowledge.queries import (
    ListKnowledgeDocumentsQuery,
    execute_list_knowledge_documents,
)
from social_reply.domain.knowledge.embeddings import FakeEmbeddingClient
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


class CountingEmbeddingClient(FakeEmbeddingClient):
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return await super().embed(texts)


class CoordinatedEmbeddingClient(FakeEmbeddingClient):
    def __init__(self, participant_count: int = 2) -> None:
        self.calls = 0
        self.participant_count = participant_count
        self.all_participants_ready = asyncio.Event()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls >= self.participant_count:
            self.all_participants_ready.set()
        await asyncio.wait_for(self.all_participants_ready.wait(), timeout=5)
        return await super().embed(texts)


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _seed_publishable_document(
    session,
    *,
    tenant_id: str = "default",
    brand_id: str = "default",
    platform: str | None = None,
    question: str = "How do I reset my password?",
    reply: str = "Open settings and choose Reset password.",
) -> models.KnowledgeDocument:
    document = models.KnowledgeDocument(
        tenant_id=tenant_id,
        brand_id=brand_id,
        platform=platform,
        question=question,
        reply=reply,
        status="draft",
        source_language="en",
        detected_language="en",
        language_detection_status="english",
        language_verified=True,
    )
    session.add(document)
    await session.flush()
    session.add(
        models.KnowledgeChunk(
            tenant_id=tenant_id,
            document_id=document.id,
            content=f"Question: {question}\nAnswer: {reply}",
            embed_text=question,
            content_hash=_content_hash(f"{tenant_id}:{question}:{reply}"),
            embedding_version="text-embedding-3-small",
            embedding=[0.01] * 1536,
        )
    )
    await session.flush()
    return document


async def _seed_knowledge_decision_reference(
    session,
    document: models.KnowledgeDocument,
    *,
    outbox_status: str | None = None,
    hash_only: bool = False,
) -> None:
    account_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session.add(
        models.PlatformAccount(
            id=account_id,
            tenant_id=document.tenant_id,
            brand_id=document.brand_id,
            platform="telegram",
            name="Knowledge decision account",
            public_id=f"knowledge-{account_id}",
            config={},
            capability={"dm": True, "max_text_length": 4096},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.flush()
    session.add(
        models.Contact(
            id=contact_id,
            tenant_id=document.tenant_id,
            platform="telegram",
            platform_account_id=account_id,
            external_user_id=f"knowledge-user-{contact_id}",
        )
    )
    await session.flush()
    session.add(
        models.Conversation(
            id=conversation_id,
            tenant_id=document.tenant_id,
            brand_id=document.brand_id,
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"knowledge:{conversation_id}",
            channel_type="dm",
        )
    )
    await session.flush()
    outbox_id = None
    if outbox_status is not None:
        outbox = models.OutboxMessage(
            tenant_id=document.tenant_id,
            conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="telegram_dm",
            destination_id="knowledge-user",
            message_type="text",
            payload={"text": "redacted"},
            idempotency_key=f"knowledge-{uuid.uuid4()}",
            status=outbox_status,
        )
        session.add(outbox)
        await session.flush()
        outbox_id = outbox.id
    chunk_id = await session.scalar(
        select(models.KnowledgeChunk.id).where(
            models.KnowledgeChunk.tenant_id == document.tenant_id,
            models.KnowledgeChunk.document_id == document.id,
        )
    )
    content_hash = await session.scalar(
        select(models.KnowledgeChunk.content_hash).where(
            models.KnowledgeChunk.tenant_id == document.tenant_id,
            models.KnowledgeChunk.document_id == document.id,
        )
    )
    session.add(
        models.ReplyDecision(
            tenant_id=document.tenant_id,
            conversation_id=conversation_id,
            action="draft",
            risk_level="low",
            confidence=1.0,
            reply_text="redacted",
            reason_codes=[],
            source="rule",
            knowledge_content_hash=content_hash,
            knowledge_document_id=None if hash_only else document.id,
            knowledge_chunk_id=None if hash_only else chunk_id,
            outbox_id=outbox_id,
        )
    )
    await session.flush()


async def test_typed_create_is_tenant_scoped_idempotent_and_audited(session, migrated_db):
    embedder = CountingEmbeddingClient()
    command = CreateKnowledgeDocumentCommand(
        required_tenant_id="default",
        actor="user:knowledge-admin",
        question="How can I change my password?",
        reply="Open Settings, then choose Password.",
        brand_id="retail",
        platform="telegram",
        category="Settings",
        protected_values=("Settings",),
    )

    document = await execute_create_knowledge_document(session, command, embedder=embedder)
    await session.commit()

    assert document.tenant_id == "default"
    assert document.status == "draft"
    assert embedder.calls == 1
    audit = await session.scalar(
        select(models.AuditLog).where(models.AuditLog.action == "CREATE_KNOWLEDGE_DOCUMENT")
    )
    assert audit is not None
    assert audit.actor == "user:knowledge-admin"
    assert audit.detail["question_length"] == len(command.question)
    assert audit.detail["reply_length"] == len(command.reply)
    assert audit.detail["content_hash"]
    assert audit.detail["category"] is None
    serialized_detail = str(audit.detail)
    assert command.question not in serialized_detail
    assert command.reply not in serialized_detail
    assert "Settings" not in serialized_detail

    with pytest.raises(KnowledgeConflictError, match="knowledge_document_duplicate"):
        await execute_create_knowledge_document(session, command, embedder=embedder)
    assert embedder.calls == 1


async def test_concurrent_typed_create_returns_one_stable_duplicate_conflict(migrated_db):
    embedder = CoordinatedEmbeddingClient()
    command = CreateKnowledgeDocumentCommand(
        required_tenant_id="default",
        actor="user:knowledge-admin",
        question="How do concurrent creates stay idempotent?",
        reply="Serialize the tenant-scoped content hash before persistence.",
        brand_id="default",
    )

    async def create_document() -> str:
        async with get_session_factory()() as concurrent_session:
            try:
                await execute_create_knowledge_document(
                    concurrent_session,
                    command,
                    embedder=embedder,
                )
                await concurrent_session.commit()
            except KnowledgeConflictError as exc:
                await concurrent_session.rollback()
                return exc.code
            return "created"

    outcomes = await asyncio.gather(create_document(), create_document())

    assert sorted(outcomes) == ["created", "knowledge_document_duplicate"]
    async with get_session_factory()() as verification_session:
        documents = list(
            await verification_session.scalars(
                select(models.KnowledgeDocument).where(
                    models.KnowledgeDocument.tenant_id == "default",
                    models.KnowledgeDocument.question == command.question,
                )
            )
        )
    assert len(documents) == 1


async def test_concurrent_batch_import_counts_duplicate_as_skip(migrated_db):
    embedder = CoordinatedEmbeddingClient()
    csv_text = (
        "question,reply,brand_id\n"
        "How do concurrent imports stay idempotent?,Skip the duplicate row.,default\n"
    )

    async def import_batch():
        async with get_session_factory()() as concurrent_session:
            report = await execute_import_knowledge_batch(
                concurrent_session,
                ImportKnowledgeBatchCommand(
                    required_tenant_id="default",
                    actor="user:knowledge-admin",
                    csv_text=csv_text,
                    source_name="concurrent.csv",
                ),
                embedder=embedder,
            )
            await concurrent_session.commit()
            return report

    reports = await asyncio.gather(import_batch(), import_batch())

    assert sorted((report.inserted, report.skipped) for report in reports) == [
        (0, 1),
        (1, 0),
    ]
    async with get_session_factory()() as verification_session:
        documents = list(
            await verification_session.scalars(
                select(models.KnowledgeDocument).where(
                    models.KnowledgeDocument.tenant_id == "default",
                    models.KnowledgeDocument.question
                    == "How do concurrent imports stay idempotent?",
                )
            )
        )
    assert len(documents) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"question": "q" * 2001},
        {"reply": "r" * 10001},
        {"brand_id": "contains spaces"},
        {"platform": "Telegram!"},
        {"category": "bad/category"},
        {"actor": ""},
        {"required_tenant_id": ""},
    ],
)
async def test_typed_create_rejects_invalid_boundary_values(session, migrated_db, changes):
    values = {
        "required_tenant_id": "default",
        "actor": "user:knowledge-admin",
        "question": "Valid question",
        "reply": "Valid reply",
        "brand_id": "default",
        "platform": None,
        "category": "faq",
    }
    values.update(changes)
    with pytest.raises(KnowledgeValidationError):
        await execute_create_knowledge_document(
            session,
            CreateKnowledgeDocumentCommand(**values),
            embedder=FakeEmbeddingClient(),
        )


async def test_confirm_english_handles_detection_states_reason_and_tenant_scope(
    session, migrated_db
):
    unknown_document = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="default",
        question="VPN 2FA",
        reply="Use VPN 2FA",
        status="draft",
        detected_language="und",
        language_detection_status="unknown",
    )
    mixed_document = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="default",
        question="How to reset?",
        reply="请打开设置",
        status="draft",
        detected_language="mixed",
        language_detection_status="mixed",
    )
    session.add_all([unknown_document, mixed_document])
    await session.commit()

    with pytest.raises(KnowledgeValidationError, match="confirmation_reason"):
        await execute_confirm_knowledge_english(
            session,
            ConfirmKnowledgeEnglishCommand(
                required_tenant_id="default",
                actor="user:knowledge-admin",
                document_id=unknown_document.id,
                confirmation_reason="short",
            ),
        )
    with pytest.raises(KnowledgeConflictError, match="english_replacement_required"):
        await execute_confirm_knowledge_english(
            session,
            ConfirmKnowledgeEnglishCommand(
                required_tenant_id="default",
                actor="user:knowledge-admin",
                document_id=mixed_document.id,
                confirmation_reason="The complete source was manually reviewed.",
            ),
        )
    with pytest.raises(KnowledgeNotFoundError):
        await execute_confirm_knowledge_english(
            session,
            ConfirmKnowledgeEnglishCommand(
                required_tenant_id="tenant-b",
                actor="user:knowledge-admin",
                document_id=unknown_document.id,
                confirmation_reason="The complete source was manually reviewed.",
            ),
        )

    confirmed = await execute_confirm_knowledge_english(
        session,
        ConfirmKnowledgeEnglishCommand(
            required_tenant_id="default",
            actor="user:knowledge-admin",
            document_id=unknown_document.id,
            confirmation_reason="The complete source was manually reviewed.",
        ),
    )
    await session.commit()
    assert confirmed.source_language == "en"
    assert confirmed.language_verified is True


async def test_confirm_english_rejects_unknown_detection_status(session, migrated_db):
    document = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="default",
        question="Parser failed",
        reply="Untrusted language state",
        status="draft",
        detected_language="und",
        language_detection_status="parser_error",
    )
    session.add(document)
    await session.commit()

    with pytest.raises(
        KnowledgeConflictError,
        match="knowledge_language_detection_status_invalid",
    ):
        await execute_confirm_knowledge_english(
            session,
            ConfirmKnowledgeEnglishCommand(
                required_tenant_id="default",
                actor="user:knowledge-admin",
                document_id=document.id,
                confirmation_reason="The complete source was manually reviewed.",
            ),
        )


async def test_review_query_uses_real_detection_states_and_preserves_filters(session, migrated_db):
    session.add_all(
        [
            models.KnowledgeDocument(
                tenant_id="default",
                brand_id="brand-a",
                platform="telegram",
                category="faq",
                question="english pending",
                reply="pending",
                status="draft",
                detected_language="en",
                language_detection_status="english",
                language_verified=False,
            ),
            models.KnowledgeDocument(
                tenant_id="default",
                brand_id="brand-a",
                platform="telegram",
                category="faq",
                question="english verified",
                reply="verified",
                status="draft",
                source_language="en",
                detected_language="en",
                language_detection_status="english",
                language_verified=True,
            ),
            models.KnowledgeDocument(
                tenant_id="default",
                brand_id="brand-b",
                platform="x",
                category="policy",
                question="unknown pending",
                reply="pending",
                status="draft",
                detected_language="und",
                language_detection_status="unknown",
                language_verified=False,
            ),
        ]
    )
    await session.commit()

    documents = await execute_list_knowledge_documents(
        session,
        ListKnowledgeDocumentsQuery(
            required_tenant_id="default",
            actor="user:knowledge-admin",
            status_filter="review",
            brand_id="brand-a",
            platform="telegram",
            category="faq",
        ),
    )

    assert [document.question for document in documents] == ["english pending"]


async def test_publish_safety_conflict_bulk_partial_and_delete_audit(session, migrated_db):
    publishable = await _seed_publishable_document(session, question="Shared question")
    contact_like = await _seed_publishable_document(
        session,
        question="Contact support",
        reply="Email help@example.com.",
    )
    missing_embedding = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="default",
        question="Missing embedding",
        reply="No current vector.",
        status="draft",
        source_language="en",
        detected_language="en",
        language_detection_status="english",
        language_verified=True,
    )
    session.add(missing_embedding)
    await session.commit()

    result = await execute_bulk_publish_knowledge(
        session,
        BulkPublishKnowledgeCommand(
            required_tenant_id="default",
            actor="user:knowledge-admin",
            document_ids=(publishable.id, contact_like.id, missing_embedding.id),
        ),
    )
    await session.commit()
    assert result.published_count == 1
    assert result.skipped_count == 2
    assert result.skip_reasons == {
        "knowledge_embedding_not_ready": 1,
        "official_contact_requires_review": 1,
    }

    conflicting = await _seed_publishable_document(
        session,
        platform="telegram",
        question="  SHARED   QUESTION ",
        reply="A conflicting answer.",
    )
    await session.commit()
    with pytest.raises(KnowledgeConflictError, match="conflicting_published_knowledge"):
        await execute_publish_knowledge(
            session,
            PublishKnowledgeCommand(
                required_tenant_id="default",
                actor="user:knowledge-admin",
                document_id=conflicting.id,
            ),
        )

    with pytest.raises(KnowledgeConflictError, match="unpublish_knowledge_before_delete"):
        await execute_delete_knowledge_draft(
            session,
            DeleteKnowledgeDraftCommand(
                required_tenant_id="default",
                actor="user:knowledge-admin",
                document_id=publishable.id,
            ),
        )
    await execute_unpublish_knowledge(
        session,
        UnpublishKnowledgeCommand(
            required_tenant_id="default",
            actor="user:knowledge-admin",
            document_id=publishable.id,
        ),
    )
    await execute_delete_knowledge_draft(
        session,
        DeleteKnowledgeDraftCommand(
            required_tenant_id="default",
            actor="user:knowledge-admin",
            document_id=publishable.id,
        ),
    )
    await session.commit()
    assert await session.get(models.KnowledgeDocument, publishable.id) is None
    delete_audit = await session.scalar(
        select(models.AuditLog).where(models.AuditLog.action == "DELETE_KNOWLEDGE_DOCUMENT")
    )
    assert delete_audit is not None
    assert "question" not in delete_audit.detail
    assert "reply" not in delete_audit.detail


async def test_publish_conflict_uses_unicode_casefold_identity(session, migrated_db):
    published = await _seed_publishable_document(
        session,
        question="Straße",
        reply="First answer.",
    )
    conflicting = await _seed_publishable_document(
        session,
        question="STRASSE",
        reply="Second answer.",
    )
    await session.commit()
    await execute_publish_knowledge(
        session,
        PublishKnowledgeCommand(
            required_tenant_id="default",
            actor="user:knowledge-admin",
            document_id=published.id,
        ),
    )
    await session.commit()

    with pytest.raises(KnowledgeConflictError, match="conflicting_published_knowledge"):
        await execute_publish_knowledge(
            session,
            PublishKnowledgeCommand(
                required_tenant_id="default",
                actor="user:knowledge-admin",
                document_id=conflicting.id,
            ),
        )


async def test_publish_rejects_verified_document_with_invalid_detection_status(
    session, migrated_db
):
    document = await _seed_publishable_document(session, question="Invalid status")
    document.language_detection_status = "parser_error"
    await session.commit()

    with pytest.raises(KnowledgeConflictError, match="confirm_english_before_publish"):
        await execute_publish_knowledge(
            session,
            PublishKnowledgeCommand(
                required_tenant_id="default",
                actor="user:knowledge-admin",
                document_id=document.id,
            ),
        )


@pytest.mark.parametrize("hash_only", [False, True])
async def test_unpublish_blocks_direct_knowledge_outbox_in_flight(session, migrated_db, hash_only):
    document = await _seed_publishable_document(session)
    document.status = "published"
    await _seed_knowledge_decision_reference(
        session,
        document,
        outbox_status="PENDING",
        hash_only=hash_only,
    )
    await session.commit()

    with pytest.raises(KnowledgeConflictError, match="knowledge_send_in_progress"):
        await execute_unpublish_knowledge(
            session,
            UnpublishKnowledgeCommand(
                required_tenant_id="default",
                actor="user:knowledge-admin",
                document_id=document.id,
            ),
        )


@pytest.mark.parametrize("hash_only", [False, True])
async def test_delete_blocks_direct_knowledge_decision_history(session, migrated_db, hash_only):
    document = await _seed_publishable_document(session, question="Historical draft")
    await _seed_knowledge_decision_reference(session, document, hash_only=hash_only)
    await session.commit()

    with pytest.raises(
        KnowledgeConflictError,
        match="knowledge_with_decision_history_is_immutable",
    ):
        await execute_delete_knowledge_draft(
            session,
            DeleteKnowledgeDraftCommand(
                required_tenant_id="default",
                actor="user:knowledge-admin",
                document_id=document.id,
            ),
        )


async def test_official_classification_is_draft_only_and_audited(session, migrated_db):
    document = await _seed_publishable_document(session)
    await session.commit()
    classified = await execute_set_knowledge_official_contact(
        session,
        SetKnowledgeOfficialContactCommand(
            required_tenant_id="default",
            actor="user:knowledge-admin",
            document_id=document.id,
            is_official_contact=True,
        ),
    )
    await session.commit()
    assert classified.is_official_contact is True
    audit = await session.scalar(
        select(models.AuditLog).where(models.AuditLog.action == "SET_KNOWLEDGE_OFFICIAL_CONTACT")
    )
    assert audit is not None


async def _seed_web_user(
    session,
    *,
    username: str,
    password: str,
    role: str,
) -> models.AdminUser:
    user = models.AdminUser(
        username=username,
        password_hash=await hash_password(password),
        tenant_id="default",
        role=role,
        must_change_password=False,
        status="active",
    )
    session.add(user)
    await session.commit()
    return user


async def _login_web(client: httpx.AsyncClient, *, username: str, password: str) -> str:
    await client.get("/auth/login")
    csrf = client.cookies["reply_admin_csrf"]
    response = await client.post(
        "/auth/login",
        data={"csrf_token": csrf, "username": username, "password": password},
    )
    assert response.status_code == 303
    return csrf


async def test_knowledge_query_scopes_users_and_treats_like_wildcards_literally(
    session,
    migrated_db,
):
    owned_user = await _seed_web_user(
        session,
        username="query-owner",
        password="query-owner-password-123",
        role="USER",
    )
    no_account_user = await _seed_web_user(
        session,
        username="query-default-only",
        password="query-default-password-123",
        role="USER",
    )
    session.add(
        models.PlatformAccount(
            tenant_id="default",
            brand_id="owned-brand",
            platform="telegram",
            owner_user_id=owned_user.id,
            name="Owned query account",
            public_id=f"query-owned-{uuid.uuid4()}",
            config={},
            capability={"dm": True, "max_text_length": 4096},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    session.add_all(
        [
            models.KnowledgeDocument(
                tenant_id="default",
                brand_id="default",
                question="shared scope token",
                reply="SHARED-SCOPE-ANSWER",
                status="published",
            ),
            models.KnowledgeDocument(
                tenant_id="default",
                brand_id="owned-brand",
                question="owned scope token",
                reply="OWNED-SCOPE-ANSWER",
                status="published",
            ),
            models.KnowledgeDocument(
                tenant_id="default",
                brand_id="foreign-brand",
                question="foreign scope token",
                reply="FOREIGN-SCOPE-ANSWER",
                status="published",
            ),
            models.KnowledgeDocument(
                tenant_id="default",
                brand_id="default",
                question="literal percent % token",
                reply="LITERAL-PERCENT-ANSWER",
                status="published",
            ),
            models.KnowledgeDocument(
                tenant_id="default",
                brand_id="default",
                question="literal under_score token",
                reply="LITERAL-UNDERSCORE-ANSWER",
                status="published",
            ),
            models.KnowledgeDocument(
                tenant_id="default",
                brand_id="owned-brand",
                question="owned draft scope token",
                reply="OWNED-DRAFT-ANSWER",
                status="draft",
            ),
        ]
    )
    await session.commit()

    async def query_as(username: str, password: str, query: str) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            csrf_token = await _login_web(
                client,
                username=username,
                password=password,
            )
            return await client.post(
                "/app/t/default/knowledge-query",
                data={"csrf_token": csrf_token, "q": query},
            )

    owner_scope = await query_as(
        owned_user.username,
        "query-owner-password-123",
        "scope token",
    )
    percent_literal = await query_as(
        owned_user.username,
        "query-owner-password-123",
        "%",
    )
    underscore_literal = await query_as(
        owned_user.username,
        "query-owner-password-123",
        "_",
    )
    default_only_scope = await query_as(
        no_account_user.username,
        "query-default-password-123",
        "scope token",
    )
    admin_scope = await query_as(
        "admin",
        "test-admin-password",
        "scope token",
    )

    assert owner_scope.status_code == 200
    assert "SHARED-SCOPE-ANSWER" in owner_scope.text
    assert "OWNED-SCOPE-ANSWER" in owner_scope.text
    assert "FOREIGN-SCOPE-ANSWER" not in owner_scope.text
    assert "OWNED-DRAFT-ANSWER" not in owner_scope.text

    assert percent_literal.status_code == 200
    assert "LITERAL-PERCENT-ANSWER" in percent_literal.text
    assert "SHARED-SCOPE-ANSWER" not in percent_literal.text
    assert "OWNED-SCOPE-ANSWER" not in percent_literal.text

    assert underscore_literal.status_code == 200
    assert "LITERAL-UNDERSCORE-ANSWER" in underscore_literal.text
    assert "SHARED-SCOPE-ANSWER" not in underscore_literal.text
    assert "OWNED-SCOPE-ANSWER" not in underscore_literal.text

    assert default_only_scope.status_code == 200
    assert "SHARED-SCOPE-ANSWER" in default_only_scope.text
    assert "OWNED-SCOPE-ANSWER" not in default_only_scope.text
    assert "FOREIGN-SCOPE-ANSWER" not in default_only_scope.text

    assert admin_scope.status_code == 200
    assert "SHARED-SCOPE-ANSWER" in admin_scope.text
    assert "OWNED-SCOPE-ANSWER" in admin_scope.text
    assert "FOREIGN-SCOPE-ANSWER" in admin_scope.text


async def test_canonical_knowledge_permissions_legacy_redirects_and_safe_html(session, migrated_db):
    await _seed_web_user(
        session,
        username="knowledge-user",
        password="knowledge-user-password-123",
        role="USER",
    )
    sensitive_document = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="secret-brand",
        platform="telegram",
        category="INTERNAL-TOKEN-123",
        question="Use INTERNAL-TOKEN-123 for official support",
        reply="Email private-contact@example.com and use INTERNAL-TOKEN-123.",
        protected_values=["INTERNAL-TOKEN-123"],
        source_file="filename-secret@example.com.csv",
        status="draft",
        is_official_contact=True,
        detected_language="en",
        language_detection_status="english",
    )
    session.add(sensitive_document)
    session.add(
        models.KnowledgeDocument(
            tenant_id="default",
            brand_id="historical-secret",
            question="Contact historical-secret@example.com",
            reply="Use HISTORICAL-PROTECTED-TOKEN.",
            protected_values=["HISTORICAL-PROTECTED-TOKEN"],
            status="published",
            is_official_contact=True,
            source_language="en",
            detected_language="en",
            language_detection_status="english",
            language_verified=True,
        )
    )
    historical_audit = models.AuditLog(
        tenant_id="default",
        category="admin_action",
        actor="user:legacy-admin",
        action="CONFIRM_KNOWLEDGE_ENGLISH",
        subject_type="knowledge_document",
        subject_id=str(sensitive_document.id),
        detail={
            "source_file": "audit-secret@example.com.csv",
            "confirmation_reason": "INTERNAL-TOKEN-123 was manually confirmed",
            "brand": "secret-brand",
            "platform": "telegram",
            "category": "INTERNAL-TOKEN-123",
        },
    )
    session.add(historical_audit)
    await session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        admin_csrf = await _login_web(
            client,
            username="admin",
            password="test-admin-password",
        )
        page = await client.get(
            "/app/t/default/knowledge?status_filter=review&brand_id=secret-brand"
            "&platform=telegram&category=INTERNAL-TOKEN-123"
        )
        detail = await client.get(f"/app/t/default/knowledge/documents/{sensitive_document.id}")
        audit_detail = await client.get(f"/app/t/default/audit/{historical_audit.id}")
        legacy = await client.get(
            "/admin/content/knowledge?brand_id=secret-brand&status_filter=review"
        )
        csrf_rejected = await client.post(
            f"/app/t/default/knowledge/documents/{sensitive_document.id}/confirm-english",
            data={"csrf_token": "wrong", "confirmation_reason": "manual review complete"},
        )
        invalid_legacy_bool = await client.post(
            "/admin/knowledge/add",
            data={
                "csrf_token": admin_csrf,
                "tenant_id": "default",
                "question": "Legacy invalid boolean",
                "reply": "Must be rejected",
                "is_official_contact": "maybe",
            },
        )
        assert page.status_code == 200
        assert "secret-brand" not in page.text
        assert "/admin/content/knowledge" not in page.text
        assert detail.status_code == 200
        assert "private-contact@example.com" not in detail.text
        assert "filename-secret@example.com" not in detail.text
        assert "INTERNAL-TOKEN-123" not in detail.text
        assert audit_detail.status_code == 200
        assert "audit-secret@example.com" not in audit_detail.text
        assert "INTERNAL-TOKEN-123" not in audit_detail.text
        assert "secret-brand" not in audit_detail.text
        assert legacy.status_code == 303
        assert legacy.headers["location"].startswith("/app/t/default/knowledge?")
        assert "brand_id=secret-brand" in legacy.headers["location"]
        assert "status_filter=review" in legacy.headers["location"]
        assert csrf_rejected.status_code == 403
        assert invalid_legacy_bool.status_code == 422
        assert admin_csrf

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        user_csrf = await _login_web(
            client,
            username="knowledge-user",
            password="knowledge-user-password-123",
        )
        forbidden = await client.get("/app/t/default/knowledge")
        query_page = await client.get("/app/t/default/knowledge-query")
        query_redirect = await client.get(
            "/app/t/default/knowledge-query?q=historical-secret@example.com"
        )
        query_results = await client.post(
            "/app/t/default/knowledge-query",
            data={"csrf_token": user_csrf, "q": "historical-secret"},
        )
        assert forbidden.status_code == 403
        assert query_page.status_code == 200
        assert query_redirect.status_code == 303
        assert query_redirect.headers["location"] == "/app/t/default/knowledge-query"
        assert query_results.status_code == 200
        assert "historical-secret@example.com" not in query_results.text
        assert "HISTORICAL-PROTECTED-TOKEN" not in query_results.text
