from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.home_overview import load_home_overview
from social_reply.infrastructure.database import models
from social_reply.shared.config import Settings

pytestmark = pytest.mark.integration
_RECORDED_AT = datetime(2026, 9, 6, 12, tzinfo=UTC)
_RECORDED_TIMESTAMP = _RECORDED_AT.isoformat()


def _principal(user_id: UUID | None = None) -> Principal:
    return Principal(
        session_id=uuid4(),
        user_id=user_id,
        username="overview-operator",
        actor="user:overview-operator",
        allowed_tenants=frozenset({"tenant-a"}),
        tenant_id="tenant-a",
        role="USER" if user_id is not None else "SUPERADMIN",
    )


def _settings(**gates: bool) -> Settings:
    return Settings(
        _env_file=None,
        testing=True,
        **{
            "facebook_messenger_enabled": True,
            "instagram_messaging_enabled": True,
            "feishu_enabled": True,
            **gates,
        },
    )


async def _conversation(
    session: AsyncSession,
    name: str,
    *,
    tenant_id: str = "tenant-a",
    owner_user_id: UUID | None = None,
    conversation_tenant_id: str | None = None,
) -> models.Conversation:
    account = models.PlatformAccount(
        id=uuid4(), tenant_id=tenant_id, brand_id="support", name=name,
        platform="facebook", owner_user_id=owner_user_id,
        config={"meta_health_status": "ERROR", "meta_health_checked_at": _RECORDED_AT.isoformat()},
    )
    session.add(account)
    await session.flush()
    contact = models.Contact(
        id=uuid4(), tenant_id=tenant_id, platform="facebook",
        platform_account_id=account.id, external_user_id=str(uuid4()),
    )
    session.add(contact)
    await session.flush()
    conversation = models.Conversation(
        id=uuid4(), tenant_id=conversation_tenant_id or tenant_id, brand_id="support",
        platform="facebook", platform_account_id=account.id,
        contact_id=contact.id, conversation_key=str(uuid4()),
    )
    session.add(conversation)
    await session.flush()
    return conversation


def _handoff(conversation: models.Conversation, *, minutes: int = 0) -> models.HumanWorkItem:
    return models.HumanWorkItem(
        id=uuid4(), tenant_id=conversation.tenant_id, conversation_id=conversation.id,
        reason_code="NEEDS_HUMAN", status="RESOLVED",
        created_at=_RECORDED_AT + timedelta(minutes=minutes),
    )


def _outbox(
    conversation: models.Conversation,
    *,
    origin: str = "DECISION",
    actor: str = "BOT",
    status: str = "SENT",
    sent_at: datetime | None = _RECORDED_AT,
    tenant_id: str | None = None,
    account_id: UUID | None = None,
) -> models.OutboxMessage:
    return models.OutboxMessage(
        id=uuid4(), tenant_id=tenant_id or conversation.tenant_id,
        conversation_id=conversation.id,
        platform_account_id=account_id or conversation.platform_account_id,
        destination_type="facebook_dm", destination_id="synthetic-destination",
        message_type="text", payload={"text": "must-not-be-returned"},
        idempotency_key=str(uuid4()), origin_kind=origin, actor_kind=actor,
        status=status, sent_at=sent_at,
        created_at=_RECORDED_AT - timedelta(days=30),
    )


async def test_home_isolates_owner_and_every_tenant_before_limiting_handoffs(session):
    owner_id, other_owner_id = uuid4(), uuid4()
    session.add_all([
        models.AdminUser(
            id=owner_id, username="overview-owner", tenant_id="tenant-a",
            password_hash="unused-test-hash", role="USER",
        ),
        models.AdminUser(
            id=other_owner_id, username="overview-other", tenant_id="tenant-a",
            password_hash="unused-test-hash", role="USER",
        ),
    ])
    await session.flush()
    owned = await _conversation(session, "Z-owned", owner_user_id=owner_id)
    other_owner = await _conversation(session, "A-other-owner", owner_user_id=other_owner_id)
    foreign = await _conversation(session, "A-other-tenant", tenant_id="tenant-b")
    wrong_account_tenant = await _conversation(
        session, "A-mismatched-account", tenant_id="tenant-b", conversation_tenant_id="tenant-a"
    )
    visible = _handoff(owned)
    session.add_all([
        visible, _handoff(foreign, minutes=50), _handoff(wrong_account_tenant, minutes=50),
        *(_handoff(other_owner, minutes=20 + offset) for offset in range(6)),
    ])
    session.add_all([
        models.AuditLog(
            tenant_id="tenant-a", category="authentication", action="LOGIN_SUCCESS",
            actor="user:overview-owner", subject_type="admin_user", subject_id=str(owner_id),
            created_at=_RECORDED_AT + timedelta(days=1),
        )
        for _index in range(8)
    ])
    await session.flush()

    overview = await load_home_overview(
        session, _principal(owner_id), "tenant-a", settings=_settings()
    )

    assert tuple(alert.account_id for alert in overview.alerts) == (owned.platform_account_id,)
    assert len(overview.activities) == 1
    assert overview.activities[0].event_id == visible.id
    assert overview.activities[0].conversation_id == owned.id
    assert overview.activities[0].kind == "handoff"
    assert overview.activities[0].occurred_at == visible.created_at
    assert "must-not-be-returned" not in repr(overview)

    missing_identity = replace(_principal(owner_id), user_id=None)
    denied = await load_home_overview(
        session, missing_identity, "tenant-a", settings=_settings()
    )
    assert denied.alerts == () and denied.activities == ()


async def test_home_delivery_activity_requires_send_or_attempt_evidence_and_consistent_scope(
    session,
):
    conversation = await _conversation(session, "Support")
    foreign = await _conversation(session, "Foreign", tenant_id="tenant-b")
    other_account = await _conversation(session, "Other-account")
    wrong_account_tenant = await _conversation(
        session, "Wrong-account-tenant", tenant_id="tenant-b", conversation_tenant_id="tenant-a"
    )
    sent_rows = [
        _outbox(conversation, origin=origin, actor=actor)
        for origin, actor in (
            ("DECISION", "BOT"), ("DRAFT_APPROVAL", "ADMIN_HUMAN"),
            ("MANUAL_REPLY", "ADMIN_HUMAN"),
        )
    ]
    failed = _outbox(conversation, status="FAILED", sent_at=None)
    review = _outbox(conversation, status="NEEDS_REVIEW", sent_at=None)
    recovered = _outbox(conversation, status="PENDING", sent_at=None)
    invalid_scopes = (
        (conversation, {"tenant_id": "tenant-b"}),
        (foreign, {"tenant_id": "tenant-a"}),
        (conversation, {"account_id": foreign.platform_account_id}),
        (conversation, {"account_id": other_account.platform_account_id}),
        (wrong_account_tenant, {}),
    )
    invalid_sent_rows = [
        _outbox(scoped_conversation, sent_at=_RECORDED_AT + timedelta(days=1), **overrides)
        for scoped_conversation, overrides in invalid_scopes
    ]
    invalid_failed_rows = [
        _outbox(scoped_conversation, status="FAILED", sent_at=None, **overrides)
        for scoped_conversation, overrides in invalid_scopes
    ]
    distractors = [
        _outbox(conversation, sent_at=None),
        _outbox(
            conversation, actor="ADMIN_HUMAN", sent_at=_RECORDED_AT + timedelta(days=1)
        ),
        _outbox(conversation, status="FAILED", sent_at=None),
        *(
            _outbox(
                conversation, origin="SYSTEM_NOTICE", actor="SYSTEM",
                sent_at=_RECORDED_AT + timedelta(days=1),
            )
            for _index in range(8)
        ),
    ]
    session.add_all([
        *sent_rows, failed, review, recovered,
        *invalid_sent_rows, *invalid_failed_rows, *distractors,
    ])
    await session.flush()
    failure_attempt = models.DeliveryAttempt(
        id=uuid4(), outbox_id=failed.id, attempt_no=1, outcome="FAILED",
        error_message="must-not-be-returned", created_at=_RECORDED_AT + timedelta(minutes=1),
    )
    review_attempt = models.DeliveryAttempt(
        id=uuid4(), outbox_id=review.id, attempt_no=1, outcome="NEEDS_REVIEW",
        created_at=_RECORDED_AT + timedelta(minutes=2),
    )
    session.add_all([
        failure_attempt, review_attempt,
        *(
            models.DeliveryAttempt(
                id=uuid4(), outbox_id=outbox.id, attempt_no=1, outcome="FAILED",
                created_at=_RECORDED_AT + timedelta(days=1),
            )
            for outbox in invalid_failed_rows
        ),
        models.DeliveryAttempt(
            id=uuid4(), outbox_id=recovered.id, attempt_no=1, outcome="FAILED",
            created_at=_RECORDED_AT + timedelta(days=1),
        ),
        models.DeliveryAttempt(
            id=uuid4(), outbox_id=sent_rows[0].id, attempt_no=1, outcome="FAILED",
            created_at=_RECORDED_AT + timedelta(days=1),
        ),
        models.DeliveryAttempt(
            id=uuid4(), outbox_id=failed.id, attempt_no=2, outcome="CANCELLED",
            created_at=_RECORDED_AT + timedelta(days=1),
        ),
    ])
    await session.flush()

    overview = await load_home_overview(session, _principal(), "tenant-a", settings=_settings())

    expected_kinds = {
        sent_rows[0].id: "auto_sent", sent_rows[1].id: "draft_sent",
        sent_rows[2].id: "manual_sent", failure_attempt.id: "delivery_failed",
        review_attempt.id: "delivery_review",
    }
    assert {event.event_id: event.kind for event in overview.activities} == expected_kinds
    assert tuple(event.event_id for event in overview.activities) == (
        review_attempt.id, failure_attempt.id,
        *sorted((outbox.id for outbox in sent_rows), reverse=True),
    )
    assert tuple(event.occurred_at for event in overview.activities) == (
        review_attempt.created_at, failure_attempt.created_at,
        *(_RECORDED_AT for _index in sent_rows),
    )
    assert all(event.conversation_id == conversation.id for event in overview.activities)
    assert "must-not-be-returned" not in repr(overview)


async def test_home_alerts_filter_persisted_faults_active_accounts_and_gates_before_limit(session):
    def account(
        name: str,
        platform: str,
        health: str | None,
        *,
        status: str = "active",
        checked_at: str = _RECORDED_TIMESTAMP,
    ) -> models.PlatformAccount:
        prefix = "feishu" if platform == "feishu" else "meta"
        return models.PlatformAccount(
            id=uuid4(), tenant_id="tenant-a", brand_id="support", name=name,
            platform=platform, status=status,
            config={} if health is None else {
                f"{prefix}_health_status": health, f"{prefix}_health_checked_at": checked_at,
                "unrelated_private_setting": "must-not-be-returned",
            },
        )

    facebook_error = account("Z-Facebook", "facebook", "ERROR")
    feishu_error = account("Z-Feishu", "feishu", "BOT_ID_MISMATCH")
    instagram_error = account("Z-Instagram", "instagram", "REAUTH_REQUIRED", checked_at="invalid")
    instagram_extra = account("ZZ-Instagram", "instagram", "ERROR")
    session.add_all([
        facebook_error, feishu_error, instagram_error, instagram_extra,
        account("A-disabled", "facebook", "ERROR", status="DISABLED"),
        account("A-ready", "facebook", "READY"),
        account("A-ready", "feishu", "READY"),
        account("A-missing", "facebook", None),
        account("A-unknown", "facebook", "UNKNOWN"),
        account("A-subscription", "instagram", "SUBSCRIPTION_MISSING"),
        account("A-email", "email", "ERROR"),
    ])
    await session.flush()

    overview = await load_home_overview(session, _principal(), "tenant-a", settings=_settings())

    assert tuple(alert.account_id for alert in overview.alerts) == (
        facebook_error.id, feishu_error.id, instagram_error.id,
    )
    assert overview.alerts[0].checked_at == _RECORDED_AT
    assert overview.alerts[2].checked_at is None
    assert overview.activities == ()
    assert "must-not-be-returned" not in repr(overview)
    gated = await load_home_overview(
        session, _principal(), "tenant-a",
        settings=_settings(facebook_messenger_enabled=False, feishu_enabled=False),
    )
    assert tuple(alert.account_id for alert in gated.alerts) == (
        instagram_error.id, instagram_extra.id,
    )
