import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, insert, select, text

from social_reply.application.account_management.human_workflow import (
    HumanWorkflowConflict,
    HumanWorkflowError,
    claim_human_work_item,
    ensure_open_human_work_item,
    resolve_human_work_item,
    resume_bot,
    send_human_reply,
)
from social_reply.application.message_delivery.intents import OutboxIdempotencyConflict
from social_reply.application.message_delivery.outbox import deliver_outbox
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

pytestmark = pytest.mark.integration


async def _seed_conversation(session, *, platform: str = "telegram") -> tuple[uuid.UUID, ...]:
    account_id, contact_id, conversation_id, message_id, work_id, bot_outbox_id = (
        uuid.uuid4() for _ in range(6)
    )
    is_feishu = platform == "feishu"
    reply_target = (
        {
            "kind": "dm",
            "message_id": "om_manual_source",
            "chat_id": "oc_manual",
            "chat_type": "p2p",
            "sender_open_id": "user-1",
        }
        if is_feishu
        else {"chat_id": 123}
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform=platform,
            name="operations-account",
            external_account_id="cli_12345678" if is_feishu else "account-1",
            public_id=f"operations-{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle(
                {"app_id": "cli_12345678", "app_secret": "secret"}
                if is_feishu
                else {"bot_token": "token"}
            ),
            config={
                "delivery_mode": "direct",
                **({"feishu_health_status": "READY"} if is_feishu else {}),
            },
            capability={
                "dm": True,
                "max_text_length": 4000 if is_feishu else 1000,
                **({"mentions": True} if is_feishu else {}),
            },
            automation_default="BOT_ACTIVE",
            status="active",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="tenant-a",
            platform=platform,
            platform_account_id=account_id,
            external_user_id="user-1",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform=platform,
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"{platform}:{account_id}:user-1",
        )
    )
    await session.execute(
        insert(models.AutomationState).values(
            conversation_id=conversation_id,
            state="HANDOFF_PENDING",
            state_version=2,
        )
    )
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="Need help",
            reply_target=reply_target,
        )
    )
    await session.execute(
        insert(models.HumanWorkItem).values(
            id=work_id,
            tenant_id="tenant-a",
            conversation_id=conversation_id,
            status="WAITING",
            reason_code="LLM_UNAVAILABLE",
            version=1,
        )
    )
    await session.execute(
        insert(models.OutboxMessage).values(
            id=bot_outbox_id,
            tenant_id="tenant-a",
            conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="feishu_p2p_reply" if is_feishu else "telegram_dm",
            destination_id="oc_manual" if is_feishu else "123",
            message_type="text",
            payload={"text": "stale bot reply", "target": reply_target},
            reply_to_message_id=message_id,
            origin_kind="DECISION",
            actor_kind="BOT",
            idempotency_key=f"bot-{bot_outbox_id}",
            status="PENDING",
        )
    )
    await session.commit()
    return account_id, conversation_id, message_id, work_id, bot_outbox_id


async def test_manual_reply_is_atomic_and_browser_idempotent(session, monkeypatch):
    account_id, conversation_id, message_id, work_id, bot_outbox_id = await _seed_conversation(
        session
    )

    async def skip_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "social_reply.application.account_management.human_workflow.dispatch_actor",
        skip_dispatch,
    )
    command = {
        "conversation_id": conversation_id,
        "reply_to_message_id": message_id,
        "text": "A human answer",
        "idempotency_key": "browser-command-0001",
        "allowed_tenants": frozenset({"tenant-a"}),
        "actor": "user:alice",
        "user_id": None,
        "allow_override": True,
        "work_item_id": work_id,
        "expected_version": 1,
    }
    first_id = await send_human_reply(**command)
    second_id = await send_human_reply(**command)
    assert second_id == first_id

    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_id)
    state = await session.get(models.AutomationState, conversation_id)
    stale_bot = await session.get(models.OutboxMessage, bot_outbox_id)
    manual = await session.get(models.OutboxMessage, first_id)
    audits = (
        (
            await session.execute(
                select(models.AuditLog).where(
                    models.AuditLog.action == "SEND_REPLY",
                    models.AuditLog.subject_id == str(conversation_id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert work.status == "CLAIMED"
    assert work.assigned_actor == "user:alice"
    assert work.version == 2
    assert state.state == "HUMAN_ACTIVE"
    assert stale_bot.status == "CANCELLED"
    assert manual.origin_kind == "MANUAL_REPLY"
    assert manual.actor_kind == "ADMIN_HUMAN"
    assert manual.actor_id == "user:alice"
    assert manual.reply_to_message_id == message_id
    assert manual.payload["target"] == {"chat_id": 123}
    assert len(audits) == 1
    assert (
        await session.execute(
            select(models.ReplyDecision).where(models.ReplyDecision.outbox_id == first_id)
        )
    ).first() is None

    with pytest.raises(OutboxIdempotencyConflict):
        await send_human_reply(**{**command, "text": "Different answer"})


async def test_feishu_manual_reply_uses_shared_sender_and_outbox_uuid(session, monkeypatch):
    account_id, conversation_id, message_id, work_id, _bot_outbox_id = await _seed_conversation(
        session, platform="feishu"
    )

    async def skip_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "social_reply.application.account_management.human_workflow.dispatch_actor",
        skip_dispatch,
    )
    outbox_id = await send_human_reply(
        conversation_id=conversation_id,
        reply_to_message_id=message_id,
        text="人工回复",
        idempotency_key="feishu-manual-command-1",
        allowed_tenants=frozenset({"tenant-a"}),
        actor="user:alice",
        user_id=None,
        allow_override=True,
        work_item_id=work_id,
        expected_version=1,
    )
    calls = []

    class Sender:
        async def send_text(self, *, target, text):
            calls.append((target, text))
            return "om_human_reply"

        async def aclose(self):
            return None

    async def get_sender(resolved_account_id):
        assert resolved_account_id == account_id
        return Sender()

    from social_reply.application.message_delivery import outbox as outbox_module

    settings = outbox_module.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(outbox_module, "get_settings", lambda: settings)
    monkeypatch.setattr(outbox_module, "get_platform_sender", get_sender)

    assert await deliver_outbox(str(outbox_id)) == "SENT"
    assert calls == [
        (
            {
                "kind": "dm",
                "message_id": "om_manual_source",
                "chat_id": "oc_manual",
                "chat_type": "p2p",
                "sender_open_id": "user-1",
                "uuid": str(outbox_id),
            },
            "人工回复",
        )
    ]


async def test_manual_reply_delivers_without_reply_decision(session, monkeypatch):
    _account_id, conversation_id, message_id, work_id, _bot_outbox_id = await _seed_conversation(
        session
    )

    async def skip_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "social_reply.application.account_management.human_workflow.dispatch_actor",
        skip_dispatch,
    )
    outbox_id = await send_human_reply(
        conversation_id=conversation_id,
        reply_to_message_id=message_id,
        text="Delivered by a person",
        idempotency_key="browser-command-0002",
        allowed_tenants=frozenset({"tenant-a"}),
        actor="user:alice",
        user_id=None,
        allow_override=True,
        work_item_id=work_id,
        expected_version=1,
    )
    sent: list[tuple[dict, str]] = []

    class Sender:
        async def send_text(self, *, target, text):
            sent.append((target, text))
            return "platform-human-1"

        async def aclose(self):
            return None

    async def get_sender(_account_id):
        return Sender()

    monkeypatch.setattr(
        "social_reply.application.message_delivery.outbox.get_platform_sender", get_sender
    )
    assert await deliver_outbox(str(outbox_id)) == "SENT"
    assert sent == [({"chat_id": 123}, "Delivered by a person")]
    session.expire_all()
    outbound = (
        await session.execute(
            select(models.Message).where(models.Message.source_outbox_id == outbox_id)
        )
    ).scalar_one()
    assert outbound.sender_type == "agent"
    state = await session.get(models.AutomationState, conversation_id)
    assert state.last_human_message_at is not None


async def test_resolve_then_resume_requires_closed_work_item(session):
    _account_id, conversation_id, _message_id, work_id, _bot_outbox_id = await _seed_conversation(
        session
    )
    with pytest.raises(HumanWorkflowConflict, match="still_open"):
        await resume_bot(
            conversation_id=conversation_id,
            allowed_tenants=frozenset({"tenant-a"}),
            actor="user:alice",
            target="BOT_DRAFT_ONLY",
        )

    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_id)
    state = await session.get(models.AutomationState, conversation_id)
    work.status = "CLAIMED"
    work.assigned_actor = "user:alice"
    work.claimed_at = datetime.now(UTC)
    work.version = 2
    state.state = "HUMAN_ACTIVE"
    await session.commit()
    await resolve_human_work_item(
        work_item_id=work_id,
        allowed_tenants=frozenset({"tenant-a"}),
        actor="user:alice",
        expected_version=2,
        allow_override=False,
    )
    await resume_bot(
        conversation_id=conversation_id,
        allowed_tenants=frozenset({"tenant-a"}),
        actor="user:alice",
        target="BOT_DRAFT_ONLY",
    )
    session.expire_all()
    state = await session.get(models.AutomationState, conversation_id)
    assert state.state == "BOT_DRAFT_ONLY"


async def test_tenant_mismatch_fails_closed_for_human_work_mutations(session, monkeypatch):
    _account_id, conversation_id, message_id, work_id, _bot_outbox_id = await _seed_conversation(
        session
    )
    await session.execute(text("ALTER TABLE human_work_items DISABLE TRIGGER ALL"))
    await session.execute(
        text("UPDATE human_work_items SET tenant_id = 'tenant-b' WHERE id = :work_id"),
        {"work_id": work_id},
    )
    await session.execute(text("ALTER TABLE human_work_items ENABLE TRIGGER ALL"))
    await session.commit()

    dispatched: list[uuid.UUID] = []

    async def capture_dispatch(*args, **_kwargs):
        dispatched.append(args[1])

    monkeypatch.setattr(
        "social_reply.application.account_management.human_workflow.dispatch_actor",
        capture_dispatch,
    )

    with pytest.raises(HumanWorkflowConflict, match="tenant_mismatch"):
        await ensure_open_human_work_item(
            session,
            tenant_id="tenant-a",
            conversation_id=conversation_id,
            reason_code="TEST",
        )
    await session.rollback()

    for operation in (claim_human_work_item, resolve_human_work_item):
        arguments = {
            "work_item_id": work_id,
            "allowed_tenants": frozenset({"tenant-b"}),
            "actor": "user:alice",
            "expected_version": 1,
        }
        if operation is claim_human_work_item:
            arguments["user_id"] = None
        else:
            arguments["allow_override"] = True
        with pytest.raises(HumanWorkflowConflict, match="tenant_mismatch"):
            await operation(**arguments)

    with pytest.raises(HumanWorkflowConflict, match="tenant_mismatch"):
        await send_human_reply(
            conversation_id=conversation_id,
            reply_to_message_id=message_id,
            text="Must not send",
            idempotency_key="tenant-mismatch-manual",
            allowed_tenants=frozenset({"tenant-a"}),
            actor="user:alice",
            user_id=None,
            allow_override=True,
            work_item_id=work_id,
            expected_version=1,
        )

    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_id)
    assert work.status == "WAITING"
    assert work.version == 1
    assert dispatched == []
    assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
    assert (
        await session.scalar(
            select(func.count())
            .select_from(models.OutboxMessage)
            .where(models.OutboxMessage.origin_kind == "MANUAL_REPLY")
        )
        == 0
    )


async def test_resume_bot_active_respects_meta_release_gate(session):
    _account_id, conversation_id, _message_id, work_id, _bot_outbox_id = await _seed_conversation(
        session, platform="facebook"
    )
    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_id)
    state = await session.get(models.AutomationState, conversation_id)
    work.status = "RESOLVED"
    work.resolved_at = state.updated_at
    state.state = "HUMAN_ACTIVE"
    await session.commit()

    with pytest.raises(HumanWorkflowError, match="meta_requires_bot_draft_only"):
        await resume_bot(
            conversation_id=conversation_id,
            allowed_tenants=frozenset({"tenant-a"}),
            actor="user:alice",
            target="BOT_ACTIVE",
        )
