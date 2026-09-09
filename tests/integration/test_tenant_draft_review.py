import asyncio
import uuid
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import func, select, update

from apps.api.main import create_app
from social_reply.application.account_management import admin_console, saas_console
from social_reply.application.account_management.auth import authenticate, hash_password
from social_reply.application.knowledge import publication as knowledge_publication
from social_reply.application.knowledge.commands import KnowledgeConflictError
from social_reply.application.knowledge.publication import (
    UnpublishKnowledgeCommand,
    execute_unpublish_knowledge,
)
from social_reply.application.message_delivery import intents as delivery_intents
from social_reply.application.message_delivery import outbox as outbox_module
from social_reply.application.reply_review import service as reply_review_service
from social_reply.application.reply_review.queries import reviewable_draft_condition
from social_reply.application.reply_review.service import DraftReviewConflict, approve_draft
from social_reply.domain.knowledge.policy import knowledge_revision_hash
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

pytestmark = pytest.mark.integration

_PASSWORD = "draft-review-password-123"


@dataclass(frozen=True)
class DraftContext:
    account_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID | None
    decision_id: uuid.UUID
    generation: int


class _OpenKillSwitch:
    async def is_disabled(self, brand_id, account_id, tenant_id="default"):
        return False


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def _seed_user(
    session,
    *,
    username: str,
    role: str,
    tenant_id: str = "default",
) -> models.AdminUser:
    user = models.AdminUser(
        username=username,
        password_hash=await hash_password(_PASSWORD),
        tenant_id=tenant_id,
        role=role,
        must_change_password=False,
        status="active",
    )
    session.add(user)
    await session.commit()
    return user


async def _login(
    client: httpx.AsyncClient,
    *,
    username: str,
    password: str = _PASSWORD,
) -> str:
    login_page = await client.get("/auth/login")
    assert login_page.status_code == 200
    csrf_token = client.cookies["reply_admin_csrf"]
    response = await client.post(
        "/auth/login",
        data={
            "csrf_token": csrf_token,
            "username": username,
            "password": password,
        },
    )
    assert response.status_code == 303
    return csrf_token


@pytest.fixture
async def knowledge_admin_principal(session):
    username = f"knowledge-draft-admin-{uuid.uuid4().hex}"
    password = "knowledge-draft-admin-password-123"
    session.add(
        models.AdminUser(
            username=username,
            password_hash=await hash_password(password),
            tenant_id="default",
            role="WORKSPACE_ADMIN",
            must_change_password=False,
            status="active",
        )
    )
    await session.commit()
    result = await authenticate(username, password)
    assert result is not None
    principal, _token = result
    assert principal.is_workspace_admin
    return principal


async def _seed_draft(
    session,
    *,
    tenant_id: str = "default",
    reply_text: str | None = "Original draft reply",
    original_reply_text: str | None = None,
    conversation_generation: int = 3,
    decision_generation: int | None = 3,
    include_message: bool = True,
    message_generation: int | None = 3,
    message_direction: str = "inbound",
    review_action: str | None = None,
    with_review_outbox: bool = False,
) -> DraftContext:
    account_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4() if include_message else None
    decision_id = uuid.uuid4()
    account = models.PlatformAccount(
        id=account_id,
        tenant_id=tenant_id,
        brand_id="default",
        platform="telegram",
        name=f"Draft account {account_id}",
        public_id=f"draft-{account_id}",
        credential_bundle=encrypt_secret_bundle({"bot_token": "provider-secret-token"}),
        config={"delivery_mode": "direct"},
        capability={"dm": True, "max_text_length": 4096},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    contact = models.Contact(
        id=contact_id,
        tenant_id=tenant_id,
        platform="telegram",
        platform_account_id=account_id,
        external_user_id=f"contact-{contact_id}",
        display_name="Draft Customer",
    )
    conversation = models.Conversation(
        id=conversation_id,
        tenant_id=tenant_id,
        brand_id="default",
        platform="telegram",
        platform_account_id=account_id,
        contact_id=contact_id,
        conversation_key=f"telegram:{account_id}:{contact_id}",
        decision_generation=conversation_generation,
    )
    session.add(account)
    await session.flush([account])
    session.add(contact)
    await session.flush([contact])
    session.add(conversation)
    await session.flush([conversation])
    session.add(
        models.AutomationState(
            conversation_id=conversation_id,
            state="BOT_DRAFT_ONLY",
            state_version=1,
        )
    )
    if message_id is not None:
        session.add(
            models.Message(
                id=message_id,
                conversation_id=conversation_id,
                direction=message_direction,
                sender_type="contact",
                text="Customer question",
                reply_target={"kind": "dm", "chat_id": 123},
                decision_generation=message_generation,
            )
        )

    review_outbox_id = None
    if with_review_outbox:
        review_outbox_id = uuid.uuid4()
        session.add(
            models.OutboxMessage(
                id=review_outbox_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                platform_account_id=account_id,
                destination_type="telegram_chat",
                destination_id=str(conversation_id),
                message_type="text",
                payload={"text": "Already reviewed", "visibility": "public", "target": {}},
                reply_to_message_id=message_id,
                origin_kind="DRAFT_APPROVAL",
                actor_kind="ADMIN_HUMAN",
                actor_id="user:previous-reviewer",
                idempotency_key=f"existing-{decision_id}",
                status="PENDING",
            )
        )

    session.add(
        models.ReplyDecision(
            id=decision_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            message_id=message_id,
            action="draft",
            reply_text=reply_text,
            original_reply_text=original_reply_text,
            review_action=review_action,
            review_outbox_id=review_outbox_id,
            reason_codes=["NEEDS_REVIEW"],
            source="rule",
            prompt_version="v1",
            decision_generation=decision_generation,
        )
    )
    await session.commit()
    return DraftContext(
        account_id=account_id,
        conversation_id=conversation_id,
        message_id=message_id,
        decision_id=decision_id,
        generation=conversation_generation,
    )


def _install_direct_sender(monkeypatch) -> list[tuple[dict, str]]:
    sent_messages: list[tuple[dict, str]] = []

    class Sender:
        async def send_text(self, *, target, text):
            sent_messages.append((target, text))
            return "platform-message-1"

        async def aclose(self):
            return None

    async def get_sender(_account_id):
        return Sender()

    monkeypatch.setattr(outbox_module, "get_platform_sender", get_sender)
    monkeypatch.setattr(outbox_module, "make_killswitch_checker", lambda: _OpenKillSwitch())
    return sent_messages


async def _seed_published_knowledge(
    session,
    *,
    question: str,
) -> tuple[models.KnowledgeDocument, models.KnowledgeChunk]:
    reply = f"Approved answer for {question}"
    content = f"问：{question}\n答：{reply}"
    document = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="default",
        question=question,
        reply=reply,
        status="published",
        source_language="en",
        detected_language="en",
        language_detection_status="english",
        language_verified=True,
    )
    session.add(document)
    await session.flush()
    chunk = models.KnowledgeChunk(
        tenant_id="default",
        document_id=document.id,
        content=content,
        embed_text=question,
        content_hash=knowledge_revision_hash(content, ()),
        embedding_version="text-embedding-3-small",
        embedding=[0.01] * 1536,
    )
    session.add(chunk)
    await session.commit()
    return document, chunk


async def _attach_knowledge_provenance(
    session,
    draft: DraftContext,
    document: models.KnowledgeDocument,
    chunk: models.KnowledgeChunk,
) -> None:
    await session.execute(
        update(models.ReplyDecision)
        .where(models.ReplyDecision.id == draft.decision_id)
        .values(
            knowledge_document_id=document.id,
            knowledge_chunk_id=chunk.id,
            knowledge_content_hash=chunk.content_hash,
        )
    )
    await session.commit()


async def test_actionable_draft_predicate_is_shared_by_admin_and_tenant_inbox(
    session, knowledge_admin_principal
) -> None:
    valid = await _seed_draft(session)
    await _seed_draft(session, decision_generation=None)
    await _seed_draft(session, decision_generation=2)
    await _seed_draft(session, reply_text="   ", original_reply_text="\t")
    await _seed_draft(session, include_message=False)
    await _seed_draft(session, message_generation=2)
    await _seed_draft(session, message_direction="outbound")
    await _seed_draft(session, review_action="REJECTED")
    await _seed_draft(session, with_review_outbox=True)
    cross_conversation_source = await _seed_draft(session)
    cross_conversation_target = await _seed_draft(session)
    await session.execute(
        update(models.ReplyDecision)
        .where(models.ReplyDecision.id == cross_conversation_source.decision_id)
        .values(message_id=None)
    )
    await session.execute(
        update(models.ReplyDecision)
        .where(models.ReplyDecision.id == cross_conversation_target.decision_id)
        .values(message_id=cross_conversation_source.message_id)
    )
    await session.commit()

    matching_ids = set(
        await session.scalars(
            select(models.ReplyDecision.id)
            .join(
                models.Conversation,
                models.ReplyDecision.conversation_id == models.Conversation.id,
            )
            .where(reviewable_draft_condition())
        )
    )
    principal = knowledge_admin_principal
    admin_summary = await admin_console._load_inbox_summary(
        session,
        frozenset({"default"}),
        tenant_id="default",
    )
    tenant_summary = await saas_console._load_inbox_summary(
        session,
        principal,
        "default",
    )
    tenant_items = await saas_console._load_inbox_items(
        session,
        principal,
        "default",
        "drafts",
    )

    assert matching_ids == {valid.decision_id}
    assert admin_summary["drafts"][0] == 1
    assert tenant_summary.draft_count == 1
    assert [item.item_id for item in tenant_items] == [valid.decision_id]


async def test_tenant_draft_panel_approves_and_legacy_admin_replay_is_idempotent(
    session,
    monkeypatch,
) -> None:
    draft = await _seed_draft(session)
    sent_messages = _install_direct_sender(monkeypatch)

    async with _client() as client:
        csrf_token = await _login(
            client,
            username="admin",
            password="test-admin-password",
        )
        page = await client.get(
            f"/app/t/default/inbox?queue=drafts&item_id={draft.decision_id}"
        )
        approval = await client.post(
            f"/app/t/default/decisions/{draft.decision_id}/approve",
            data={
                "csrf_token": csrf_token,
                "final_reply_text": "Edited tenant reply",
                "expected_generation": str(draft.generation),
                "expected_review_action": "PENDING",
            },
        )
        legacy_replay = await client.post(
            f"/admin/decisions/{draft.decision_id}/approve",
            data={
                "csrf_token": csrf_token,
                "final_reply_text": "Edited tenant reply",
            },
        )
        conflicting_replay = await client.post(
            f"/admin/decisions/{draft.decision_id}/approve",
            data={
                "csrf_token": csrf_token,
                "final_reply_text": "Different reply",
            },
        )

    assert page.status_code == 200
    assert "Original draft reply" in page.text
    assert f'action="/app/t/default/decisions/{draft.decision_id}/approve"' in page.text
    assert f'action="/app/t/default/decisions/{draft.decision_id}/discard"' in page.text
    assert 'name="expected_generation" value="3"' in page.text
    assert 'name="expected_review_action" value="PENDING"' in page.text
    assert "provider-secret-token" not in page.text
    assert approval.status_code == 303
    assert approval.headers["location"] == "/app/t/default/inbox?queue=drafts"
    assert legacy_replay.status_code == 303
    assert legacy_replay.headers["location"] == "/admin/inbox?queue=drafts"
    assert conflicting_replay.status_code == 409
    assert conflicting_replay.json() == {"detail": "draft_approval_conflict"}
    assert sent_messages == [({"kind": "dm", "chat_id": 123}, "Edited tenant reply")]

    outbox_count = await session.scalar(
        select(func.count())
        .select_from(models.OutboxMessage)
        .where(models.OutboxMessage.origin_kind == "DRAFT_APPROVAL")
    )
    audit_count = await session.scalar(
        select(func.count())
        .select_from(models.AuditLog)
        .where(
            models.AuditLog.action == "APPROVE_DRAFT",
            models.AuditLog.subject_id == str(draft.decision_id),
        )
    )
    assert outbox_count == 1
    assert audit_count == 1


@pytest.mark.parametrize(
    ("reply_text", "expected_status"),
    (("", 422), ("x" * 10000, 303), ("x" * 10001, 422)),
)
async def test_tenant_draft_approval_validates_reply_length_boundaries(
    session,
    monkeypatch,
    reply_text: str,
    expected_status: int,
) -> None:
    draft = await _seed_draft(session)
    _install_direct_sender(monkeypatch)
    monkeypatch.setattr(
        delivery_intents,
        "capability_text_limit",
        lambda _platform, _capability: 10000,
    )

    async with _client() as client:
        csrf_token = await _login(
            client,
            username="admin",
            password="test-admin-password",
        )
        response = await client.post(
            f"/app/t/default/decisions/{draft.decision_id}/approve",
            data={
                "csrf_token": csrf_token,
                "final_reply_text": reply_text,
                "expected_generation": str(draft.generation),
                "expected_review_action": "PENDING",
            },
        )

    assert response.status_code == expected_status


async def test_tenant_draft_routes_enforce_role_csrf_scope_and_stale_generation(
    session,
) -> None:
    ordinary_user = await _seed_user(session, username="scope-user", role="USER")
    default_draft = await _seed_draft(session)
    superadmin_draft = await _seed_draft(session)
    other_tenant_draft = await _seed_draft(session, tenant_id="tenant-a")

    async with _client() as client:
        csrf_token = await _login(client, username=ordinary_user.username)
        user_response = await client.post(
            f"/app/t/default/decisions/{default_draft.decision_id}/discard",
            data={
                "csrf_token": csrf_token,
                "review_reason": "Not suitable",
                "expected_generation": str(default_draft.generation),
                "expected_review_action": "PENDING",
            },
        )
    async with _client() as client:
        superadmin_csrf = await _login(
            client,
            username="admin",
            password="test-admin-password",
        )
        superadmin_response = await client.post(
            f"/app/t/default/decisions/{superadmin_draft.decision_id}/discard",
            data={
                "csrf_token": superadmin_csrf,
                "review_reason": "Not suitable",
                "expected_generation": str(superadmin_draft.generation),
                "expected_review_action": "PENDING",
            },
        )
    async with _client() as client:
        csrf_token = await _login(
            client,
            username="admin",
            password="test-admin-password",
        )
        csrf_response = await client.post(
            f"/app/t/default/decisions/{default_draft.decision_id}/discard",
            data={
                "csrf_token": "wrong-token",
                "review_reason": "Not suitable",
                "expected_generation": str(default_draft.generation),
                "expected_review_action": "PENDING",
            },
        )
        cross_tenant_response = await client.post(
            f"/app/t/default/decisions/{other_tenant_draft.decision_id}/discard",
            data={
                "csrf_token": csrf_token,
                "review_reason": "Not suitable",
                "expected_generation": str(other_tenant_draft.generation),
                "expected_review_action": "PENDING",
            },
        )
        await session.execute(
            update(models.Conversation)
            .where(models.Conversation.id == default_draft.conversation_id)
            .values(decision_generation=default_draft.generation + 1)
        )
        await session.commit()
        stale_response = await client.post(
            f"/app/t/default/decisions/{default_draft.decision_id}/discard",
            data={
                "csrf_token": csrf_token,
                "review_reason": "Not suitable",
                "expected_generation": str(default_draft.generation),
                "expected_review_action": "PENDING",
            },
        )

    assert user_response.status_code == 403
    assert superadmin_response.status_code == 303
    assert csrf_response.status_code == 403
    assert cross_tenant_response.status_code == 404
    assert stale_response.status_code == 409


async def test_tenant_draft_approval_rechecks_prompt_provenance(session) -> None:
    draft = await _seed_draft(session)
    await session.execute(
        update(models.ReplyDecision)
        .where(models.ReplyDecision.id == draft.decision_id)
        .values(reply_business_prompt_content_hash="a" * 64)
    )
    await session.commit()

    async with _client() as client:
        csrf_token = await _login(
            client,
            username="admin",
            password="test-admin-password",
        )
        response = await client.post(
            f"/app/t/default/decisions/{draft.decision_id}/approve",
            data={
                "csrf_token": csrf_token,
                "final_reply_text": "Prompt-fenced reply",
                "expected_generation": str(draft.generation),
                "expected_review_action": "PENDING",
            },
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "draft_business_prompt_disabled"}
    assert await session.scalar(select(func.count()).select_from(models.OutboxMessage)) == 0


async def test_tenant_draft_approval_rechecks_knowledge_document_chunk_and_hash(
    session,
) -> None:
    unpublished_draft = await _seed_draft(session)
    mismatched_hash_draft = await _seed_draft(session)
    mismatched_chunk_content_draft = await _seed_draft(session)
    partial_provenance_draft = await _seed_draft(session)
    unpublished_document, unpublished_chunk = await _seed_published_knowledge(
        session,
        question="Unpublished approval source",
    )
    mismatched_document, mismatched_chunk = await _seed_published_knowledge(
        session,
        question="Changed approval source",
    )
    mismatched_content_document, mismatched_content_chunk = await _seed_published_knowledge(
        session,
        question="Changed chunk content source",
    )
    partial_document, _partial_chunk = await _seed_published_knowledge(
        session,
        question="Partial approval source",
    )
    await _attach_knowledge_provenance(
        session,
        unpublished_draft,
        unpublished_document,
        unpublished_chunk,
    )
    await _attach_knowledge_provenance(
        session,
        mismatched_hash_draft,
        mismatched_document,
        mismatched_chunk,
    )
    await _attach_knowledge_provenance(
        session,
        mismatched_chunk_content_draft,
        mismatched_content_document,
        mismatched_content_chunk,
    )
    await session.execute(
        update(models.ReplyDecision)
        .where(models.ReplyDecision.id == partial_provenance_draft.decision_id)
        .values(knowledge_document_id=partial_document.id)
    )
    await session.execute(
        update(models.KnowledgeDocument)
        .where(models.KnowledgeDocument.id == unpublished_document.id)
        .values(status="draft")
    )
    await session.execute(
        update(models.KnowledgeChunk)
        .where(models.KnowledgeChunk.id == mismatched_chunk.id)
        .values(content_hash="f" * 64)
    )
    await session.execute(
        update(models.KnowledgeChunk)
        .where(models.KnowledgeChunk.id == mismatched_content_chunk.id)
        .values(content="Changed chunk body without a matching revision hash")
    )
    await session.commit()

    async with _client() as client:
        csrf_token = await _login(
            client,
            username="admin",
            password="test-admin-password",
        )

        async def approve(draft: DraftContext) -> httpx.Response:
            return await client.post(
                f"/app/t/default/decisions/{draft.decision_id}/approve",
                data={
                    "csrf_token": csrf_token,
                    "final_reply_text": "Knowledge-fenced reply",
                    "expected_generation": str(draft.generation),
                    "expected_review_action": "PENDING",
                },
            )

        responses = [
            await approve(unpublished_draft),
            await approve(mismatched_hash_draft),
            await approve(mismatched_chunk_content_draft),
            await approve(partial_provenance_draft),
        ]

    assert [response.status_code for response in responses] == [409, 409, 409, 409]
    assert [response.json() for response in responses] == [
        {"detail": "draft_knowledge_provenance_stale"},
        {"detail": "draft_knowledge_provenance_stale"},
        {"detail": "draft_knowledge_provenance_stale"},
        {"detail": "draft_knowledge_provenance_invalid"},
    ]
    assert await session.scalar(select(func.count()).select_from(models.OutboxMessage)) == 0


async def test_unpublish_and_draft_approval_serialize_both_race_orders(
    session,
    monkeypatch,
    knowledge_admin_principal,
) -> None:
    async def suppress_dispatch(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(reply_review_service, "dispatch_actor", suppress_dispatch)

    reviewer = await _seed_user(
        session,
        username="knowledge-race-reviewer",
        role="WORKSPACE_ADMIN",
    )
    authenticated = await authenticate(reviewer.username, _PASSWORD)
    assert authenticated is not None
    reviewer_principal, _token = authenticated
    # Distinct staff/session locks ensure the race exercises knowledge safety locks.
    assert reviewer_principal.user_id != knowledge_admin_principal.user_id

    unpublish_first_draft = await _seed_draft(session)
    unpublish_first_document, unpublish_first_chunk = await _seed_published_knowledge(
        session,
        question="Unpublish wins the race",
    )
    await _attach_knowledge_provenance(
        session,
        unpublish_first_draft,
        unpublish_first_document,
        unpublish_first_chunk,
    )
    approval_waiting_on_knowledge = asyncio.Event()
    original_shared_lock = reply_review_service.acquire_shared_xact_lock

    async def observe_approval_knowledge_lock(lock_session, lock_key):
        approval_waiting_on_knowledge.set()
        await original_shared_lock(lock_session, lock_key)

    monkeypatch.setattr(
        reply_review_service, "acquire_shared_xact_lock", observe_approval_knowledge_lock
    )
    approval_task = None
    try:
        async with get_session_factory()() as unpublish_session:
            await execute_unpublish_knowledge(
                unpublish_session,
                UnpublishKnowledgeCommand(
                    required_tenant_id="default",
                    principal=knowledge_admin_principal,
                    document_id=unpublish_first_document.id,
                ),
            )
            approval_task = asyncio.create_task(
                approve_draft(
                    decision_id=unpublish_first_draft.decision_id,
                    required_tenant_id="default",
                    actor=reviewer_principal.actor,
                    principal=reviewer_principal,
                    final_reply_text=None,
                    expected_generation=unpublish_first_draft.generation,
                    expected_review_action="PENDING",
                )
            )
            await asyncio.wait_for(approval_waiting_on_knowledge.wait(), timeout=5)
            approval_waited_for_unpublish = not approval_task.done()
            await unpublish_session.commit()

        with pytest.raises(
            DraftReviewConflict,
            match="draft_knowledge_provenance_stale",
        ):
            await asyncio.wait_for(approval_task, timeout=5)
    finally:
        if approval_task is not None:
            if not approval_task.done():
                approval_task.cancel()
            await asyncio.gather(approval_task, return_exceptions=True)
    assert approval_waited_for_unpublish is True

    approval_first_draft = await _seed_draft(session)
    approval_first_document, approval_first_chunk = await _seed_published_knowledge(
        session,
        question="Approval wins the race",
    )
    await _attach_knowledge_provenance(
        session,
        approval_first_draft,
        approval_first_document,
        approval_first_chunk,
    )
    approval_created_outbox = asyncio.Event()
    release_approval = asyncio.Event()
    original_create_outbox_intent = reply_review_service.create_or_get_outbox_intent

    async def pause_after_outbox_creation(*args, **kwargs):
        outbox_id = await original_create_outbox_intent(*args, **kwargs)
        approval_created_outbox.set()
        await release_approval.wait()
        return outbox_id

    monkeypatch.setattr(
        reply_review_service,
        "create_or_get_outbox_intent",
        pause_after_outbox_creation,
    )
    unpublish_waiting_on_knowledge = asyncio.Event()
    original_exclusive_lock = knowledge_publication.acquire_xact_lock

    async def observe_unpublish_knowledge_lock(lock_session, lock_key):
        unpublish_waiting_on_knowledge.set()
        await original_exclusive_lock(lock_session, lock_key)

    monkeypatch.setattr(
        knowledge_publication, "acquire_xact_lock", observe_unpublish_knowledge_lock
    )

    async def attempt_unpublish() -> str:
        async with get_session_factory()() as concurrent_session:
            try:
                await execute_unpublish_knowledge(
                    concurrent_session,
                    UnpublishKnowledgeCommand(
                        required_tenant_id="default",
                        principal=knowledge_admin_principal,
                        document_id=approval_first_document.id,
                    ),
                )
                await concurrent_session.commit()
            except KnowledgeConflictError as exc:
                await concurrent_session.rollback()
                return exc.code
            return "unpublished"

    approval_task = asyncio.create_task(
        approve_draft(
            decision_id=approval_first_draft.decision_id,
            required_tenant_id="default",
            actor=reviewer_principal.actor,
            principal=reviewer_principal,
            final_reply_text=None,
            expected_generation=approval_first_draft.generation,
            expected_review_action="PENDING",
        )
    )
    unpublish_task = None
    try:
        await asyncio.wait_for(approval_created_outbox.wait(), timeout=5)
        unpublish_task = asyncio.create_task(attempt_unpublish())
        await asyncio.wait_for(unpublish_waiting_on_knowledge.wait(), timeout=5)
        unpublish_waited_for_approval = not unpublish_task.done()
        release_approval.set()
        approval_result = await asyncio.wait_for(approval_task, timeout=5)
        unpublish_outcome = await asyncio.wait_for(unpublish_task, timeout=5)
    finally:
        release_approval.set()
        tasks = tuple(task for task in (approval_task, unpublish_task) if task is not None)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert approval_result.created is True
    assert unpublish_waited_for_approval is True
    assert unpublish_outcome == "knowledge_send_in_progress"
    async with get_session_factory()() as verification_session:
        final_status = await verification_session.scalar(
            select(models.KnowledgeDocument.status).where(
                models.KnowledgeDocument.id == approval_first_document.id
            )
        )
    assert final_status == "published"


async def test_reject_is_idempotent_and_conflicts_with_later_approval(
    session,
    monkeypatch,
) -> None:
    draft = await _seed_draft(session)
    _install_direct_sender(monkeypatch)

    async with _client() as client:
        csrf_token = await _login(
            client,
            username="admin",
            password="test-admin-password",
        )
        rejection = await client.post(
            f"/app/t/default/decisions/{draft.decision_id}/discard",
            data={
                "csrf_token": csrf_token,
                "review_reason": "Incorrect tone",
                "expected_generation": str(draft.generation),
                "expected_review_action": "PENDING",
            },
        )
        rejection_replay = await client.post(
            f"/admin/decisions/{draft.decision_id}/discard",
            data={
                "csrf_token": csrf_token,
                "review_reason": "Incorrect tone",
            },
        )
        approval_conflict = await client.post(
            f"/app/t/default/decisions/{draft.decision_id}/approve",
            data={
                "csrf_token": csrf_token,
                "final_reply_text": "Should not send",
                "expected_generation": str(draft.generation),
                "expected_review_action": "PENDING",
            },
        )

    assert rejection.status_code == 303
    assert rejection_replay.status_code == 303
    assert approval_conflict.status_code == 409
    session.expire_all()
    decision = await session.get(models.ReplyDecision, draft.decision_id)
    assert decision is not None
    assert decision.review_action == "REJECTED"
    assert decision.reason_codes.count("ADMIN_DISCARDED") == 1
    audit_count = await session.scalar(
        select(func.count())
        .select_from(models.AuditLog)
        .where(
            models.AuditLog.action == "REJECT_DRAFT",
            models.AuditLog.subject_id == str(draft.decision_id),
        )
    )
    assert audit_count == 1
