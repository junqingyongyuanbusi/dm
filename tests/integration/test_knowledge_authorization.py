"""Knowledge write authorization contracts (not run in the local static-only workflow)."""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from social_reply.application.account_management.access import lock_user_authority
from social_reply.application.account_management.auth import (
    authenticate,
    hash_password,
    issue_session,
    principal_from_session_id,
    revoke_session,
)
from social_reply.application.knowledge.authorization import _trusted_system_import_capability
from social_reply.application.knowledge.commands import (
    ConfirmKnowledgeEnglishBatchCommand,
    ConfirmKnowledgeEnglishCommand,
    CreateKnowledgeDocumentCommand,
    DeleteKnowledgeDraftCommand,
    ImportKnowledgeBatchCommand,
    KnowledgeAuthorizationError,
    SetKnowledgeOfficialContactCommand,
    execute_confirm_knowledge_english,
    execute_confirm_knowledge_english_batch,
    execute_create_knowledge_document,
    execute_delete_knowledge_draft,
    execute_import_knowledge_batch,
    execute_set_knowledge_official_contact,
)
from social_reply.application.knowledge.importer import (
    _import_knowledge_rows_system as import_knowledge_rows,
)
from social_reply.application.knowledge.importer import (
    import_knowledge_rows as ambient_import_knowledge_rows,
)
from social_reply.application.knowledge.publication import (
    BulkPublishKnowledgeCommand,
    PublishKnowledgeCommand,
    UnpublishKnowledgeCommand,
    execute_bulk_publish_knowledge,
    execute_publish_knowledge,
    execute_unpublish_knowledge,
)
from social_reply.domain.knowledge.embeddings import FakeEmbeddingClient
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration

_PASSWORD = "knowledge-authorization-password-123"

@dataclass(frozen=True)
class _PreparedOperation:
    document_ids: tuple[uuid.UUID, ...]
    snapshots: dict[uuid.UUID, tuple[str, bool, bool, str | None, str | None]]
    import_batch_id: uuid.UUID | None = None
async def _named_workspace_admin(session, *, role: str = "WORKSPACE_ADMIN"):
    username = f"knowledge-contract-{uuid.uuid4().hex}"
    user = models.AdminUser(
        username=username,
        password_hash=await hash_password(_PASSWORD),
        tenant_id="default",
        role=role,
        must_change_password=False,
        status="active",
    )
    session.add(user)
    await session.commit()
    result = await authenticate(username, _PASSWORD)
    assert result is not None
    principal, token = result
    return user, principal, token


async def _bootstrap_principal():
    _token, session_id = await issue_session()
    principal = await principal_from_session_id(session_id)
    assert principal is not None
    assert principal.is_superadmin
    assert principal.user_id is None
    assert principal.tenant_id is None
    return principal


async def _seed_document(
    session,
    *,
    tenant_id: str = "default",
    status: str = "draft",
    language_verified: bool = True,
    official: bool = False,
    question: str | None = None,
) -> models.KnowledgeDocument:
    document = models.KnowledgeDocument(
        tenant_id=tenant_id,
        brand_id="default",
        question=question or f"Knowledge contract {uuid.uuid4().hex}",
        reply="Use the documented support procedure.",
        status=status,
        source_language="en",
        detected_language="en",
        language_detection_status="english",
        language_verified=language_verified,
        is_official_contact=official,
    )
    session.add(document)
    await session.flush()
    session.add(
        models.KnowledgeChunk(
            tenant_id=tenant_id,
            document_id=document.id,
            content=f"Question: {document.question}\nAnswer: {document.reply}",
            embed_text=document.question,
            content_hash=uuid.uuid4().hex,
            embedding_version=get_settings().openai_embedding_model,
            embedding=[0.01] * 1536,
        )
    )
    await session.flush()
    return document


async def _knowledge_audits(session):
    rows = await session.execute(
        select(
            models.AuditLog.action,
            models.AuditLog.actor,
            models.AuditLog.subject_type,
            models.AuditLog.subject_id,
            models.AuditLog.detail,
        )
        .where(models.AuditLog.category == "admin_action")
        .order_by(models.AuditLog.id)
    )
    return rows.all()


async def _change_named_admin_authority(user_id: uuid.UUID, mode: str) -> None:
    async with get_session_factory()() as control_session:
        await lock_user_authority(control_session, user_id)
        user = await control_session.get(models.AdminUser, user_id, with_for_update=True)
        assert user is not None
        if mode == "disable":
            user.status = "disabled"
        elif mode == "downgrade":
            user.role = "USER"
        else:
            raise AssertionError(f"unexpected authority mode: {mode}")
        await control_session.commit()


def _document_snapshot(
    document: models.KnowledgeDocument,
) -> tuple[str, bool, bool, str | None, str | None]:
    return (
        document.status,
        document.language_verified,
        document.is_official_contact,
        document.source_language,
        document.category,
    )


async def _prepare_stale_operation(session, operation: str) -> _PreparedOperation:
    if operation in {"create", "import"}:
        documents = tuple(
            (
                await session.scalars(
                    select(models.KnowledgeDocument).order_by(models.KnowledgeDocument.id)
                )
            ).all()
        )
        return _PreparedOperation(
            document_ids=tuple(document.id for document in documents),
            snapshots={document.id: _document_snapshot(document) for document in documents},
        )
    if operation == "bulk_confirm":
        import_batch_id = uuid.uuid4()
        first = await _seed_document(session, language_verified=False)
        second = await _seed_document(session, language_verified=False)
        first.import_batch_id = import_batch_id
        second.import_batch_id = import_batch_id
        await session.commit()
        documents = (first, second)
    elif operation == "confirm":
        documents = (await _seed_document(session, language_verified=False),)
        await session.commit()
        import_batch_id = None
    elif operation == "classify":
        documents = (await _seed_document(session, official=False),)
        await session.commit()
        import_batch_id = None
    elif operation == "delete":
        documents = (await _seed_document(session),)
        await session.commit()
        import_batch_id = None
    elif operation == "publish":
        documents = (await _seed_document(session),)
        await session.commit()
        import_batch_id = None
    elif operation == "unpublish":
        documents = (await _seed_document(session, status="published"),)
        await session.commit()
        import_batch_id = None
    elif operation == "bulk":
        documents = (await _seed_document(session), await _seed_document(session))
        await session.commit()
        import_batch_id = None
    else:
        raise AssertionError(f"unexpected knowledge operation: {operation}")
    return _PreparedOperation(
        document_ids=tuple(document.id for document in documents),
        snapshots={document.id: _document_snapshot(document) for document in documents},
        import_batch_id=import_batch_id,
    )


async def _attempt_stale_operation(
    session, principal, operation: str, prepared: _PreparedOperation
):
    if operation == "create":
        return await execute_create_knowledge_document(
            session,
            CreateKnowledgeDocumentCommand(
                required_tenant_id="default",
                principal=principal,
                question="stale create",
                reply="must not persist",
            ),
            embedder=FakeEmbeddingClient(),
        )
    if operation == "import":
        return await execute_import_knowledge_batch(
            session,
            ImportKnowledgeBatchCommand(
                required_tenant_id="default",
                principal=principal,
                csv_text="question,reply\nstale import,must not persist\n",
                source_name="stale.csv",
            ),
            embedder=FakeEmbeddingClient(),
        )
    if operation == "bulk_confirm":
        assert prepared.import_batch_id is not None
        return await execute_confirm_knowledge_english_batch(
            session,
            ConfirmKnowledgeEnglishBatchCommand(
                required_tenant_id="default",
                principal=principal,
                import_batch_id=prepared.import_batch_id,
            ),
        )
    document_id = prepared.document_ids[0]
    if operation == "confirm":
        return await execute_confirm_knowledge_english(
            session,
            ConfirmKnowledgeEnglishCommand(
                required_tenant_id="default",
                principal=principal,
                document_id=document_id,
                confirmation_reason="reviewed by support",
            ),
        )
    if operation == "classify":
        return await execute_set_knowledge_official_contact(
            session,
            SetKnowledgeOfficialContactCommand(
                required_tenant_id="default",
                principal=principal,
                document_id=document_id,
                is_official_contact=True,
            ),
        )
    if operation == "delete":
        return await execute_delete_knowledge_draft(
            session,
            DeleteKnowledgeDraftCommand(
                required_tenant_id="default",
                principal=principal,
                document_id=document_id,
            ),
        )
    if operation == "publish":
        return await execute_publish_knowledge(
            session,
            PublishKnowledgeCommand(
                required_tenant_id="default",
                principal=principal,
                document_id=document_id,
            ),
        )
    if operation == "unpublish":
        return await execute_unpublish_knowledge(
            session,
            UnpublishKnowledgeCommand(
                required_tenant_id="default",
                principal=principal,
                document_id=document_id,
            ),
        )
    if operation == "bulk":
        return await execute_bulk_publish_knowledge(
            session,
            BulkPublishKnowledgeCommand(
                required_tenant_id="default",
                principal=principal,
                document_ids=prepared.document_ids,
            ),
        )
    raise AssertionError(f"unexpected knowledge operation: {operation}")


@pytest.mark.parametrize("authority_mode", ["revoke", "disable", "downgrade"])
@pytest.mark.parametrize(
    "operation",
    [
        "create",
        "import",
        "confirm",
        "bulk_confirm",
        "delete",
        "classify",
        "publish",
        "unpublish",
        "bulk",
    ],
)
async def test_stale_named_admin_cannot_mutate_knowledge(
    session, migrated_db, authority_mode, operation
):
    user, principal, token = await _named_workspace_admin(session)
    user_id = user.id
    await session.rollback()
    prepared = await _prepare_stale_operation(session, operation)
    before = await _knowledge_audits(session)
    await session.rollback()
    if authority_mode == "revoke":
        await revoke_session(token)
    else:
        await _change_named_admin_authority(user_id, authority_mode)

    with pytest.raises(KnowledgeAuthorizationError):
        await _attempt_stale_operation(session, principal, operation, prepared)
    await session.rollback()

    after = await _knowledge_audits(session)
    assert after == before
    current_ids = set(
        (await session.scalars(select(models.KnowledgeDocument.id))).all()
    )
    assert current_ids == set(prepared.document_ids)
    for document_id, snapshot in prepared.snapshots.items():
        document = await session.get(models.KnowledgeDocument, document_id)
        assert document is not None
        assert _document_snapshot(document) == snapshot

async def test_named_workspace_admin_succeeds_and_user_is_rejected(session, migrated_db):
    _admin, admin_principal, _admin_token = await _named_workspace_admin(session)
    document = await execute_create_knowledge_document(
        session,
        CreateKnowledgeDocumentCommand(
            required_tenant_id="default",
            principal=admin_principal,
            question="Named admin can create",
            reply="The named admin is reloaded from its session.",
        ),
        embedder=FakeEmbeddingClient(),
    )
    document_id = document.id
    await session.commit()

    _user, user_principal, _user_token = await _named_workspace_admin(session, role="USER")
    before = await _knowledge_audits(session)
    with pytest.raises(KnowledgeAuthorizationError):
        await execute_set_knowledge_official_contact(
            session,
            SetKnowledgeOfficialContactCommand(
                required_tenant_id="default",
                principal=user_principal,
                document_id=document_id,
                is_official_contact=True,
            ),
        )
    await session.rollback()
    unchanged = await session.get(models.KnowledgeDocument, document_id)
    assert unchanged is not None
    assert unchanged.is_official_contact is False
    assert await _knowledge_audits(session) == before


async def test_bootstrap_principal_can_write_an_allowed_actual_tenant(session, migrated_db):
    principal = await _bootstrap_principal()
    document = await _seed_document(session, tenant_id="tenant-a")
    await session.commit()

    published = await execute_publish_knowledge(
        session,
        PublishKnowledgeCommand(
            required_tenant_id="tenant-a",
            principal=principal,
            document_id=document.id,
        ),
    )
    await session.commit()
    assert published.status == "published"


async def test_system_import_requires_opaque_capability_and_marks_audit(
    session, migrated_db
):
    with pytest.raises(KnowledgeAuthorizationError):
        await ambient_import_knowledge_rows(
            io.StringIO("question,reply\nforged actor,rejected\n"),
            actor="system:knowledge-import",
        )

    with pytest.raises(KnowledgeAuthorizationError):
        await execute_import_knowledge_batch(
            session,
            ImportKnowledgeBatchCommand(
                required_tenant_id="default",
                principal=None,
                csv_text="question,reply\nno identity,rejected\n",
                source_name="rejected.csv",
            ),
            embedder=FakeEmbeddingClient(),
        )

    with pytest.raises(KnowledgeAuthorizationError):
        await execute_import_knowledge_batch(
            session,
            ImportKnowledgeBatchCommand(
                required_tenant_id="default",
                principal=None,
                csv_text="question,reply\nforged system,rejected\n",
                source_name="forged.csv",
                system_import_capability="system:knowledge-import",
            ),
            embedder=FakeEmbeddingClient(),
        )

    report = await import_knowledge_rows(
        io.StringIO("question,reply\ntrusted system,accepted\n"),
        source_name="trusted.csv",
        embedder=FakeEmbeddingClient(),
    )
    assert report.inserted == 1
    audit = await session.scalar(
        select(models.AuditLog).where(models.AuditLog.action == "IMPORT_KNOWLEDGE_BATCH")
    )
    assert audit is not None
    assert audit.actor == "system:knowledge-import"
    assert _trusted_system_import_capability() is not None
