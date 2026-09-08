import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from apps.api.main import create_app
from social_reply.application.account_management.auth import (
    hash_password,
    issue_session,
    principal_from_session_id,
)
from social_reply.application.message_delivery import recovery
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration

_PASSWORD = "delivery-recovery-password-123"


@pytest.fixture
async def recovery_principal(session):
    _token, session_id = await issue_session()
    principal = await principal_from_session_id(session_id)
    assert principal is not None and principal.is_superadmin
    return principal


@dataclass(frozen=True)
class DeliveryContext:
    tenant_id: str
    account_id: uuid.UUID
    conversation_id: uuid.UUID
    outbox_id: uuid.UUID


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


async def _seed_delivery(
    session,
    *,
    tenant_id: str = "default",
    platform: str = "telegram",
    status: str = "FAILED",
    attempt_count: int = 2,
    error_code: str = "PROVIDER_REJECTED",
    actor_kind: str = "BOT",
) -> DeliveryContext:
    account_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    inbound_message_id = uuid.uuid4()
    outbox_id = uuid.uuid4()
    account = models.PlatformAccount(
        id=account_id,
        tenant_id=tenant_id,
        brand_id="default",
        platform=platform,
        name=f"Delivery account {account_id}",
        public_id=f"delivery-{account_id}",
        credential_bundle={"ciphertext": "provider-secret-token"},
        config={"delivery_mode": "direct"},
        capability={"dm": True, "max_text_length": 4096},
        automation_default="BOT_ACTIVE",
        status="active",
    )
    contact = models.Contact(
        id=contact_id,
        tenant_id=tenant_id,
        platform=platform,
        platform_account_id=account_id,
        external_user_id=f"contact-{contact_id}",
        display_name="Delivery Customer",
    )
    conversation = models.Conversation(
        id=conversation_id,
        tenant_id=tenant_id,
        brand_id="default",
        platform=platform,
        platform_account_id=account_id,
        contact_id=contact_id,
        conversation_key=f"{platform}:{account_id}:{contact_id}",
        decision_generation=1,
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
            state="BOT_ACTIVE",
            state_version=1,
        )
    )
    session.add(
        models.Message(
            id=inbound_message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="full-customer-body-secret",
            reply_target={"chat_id": "provider-target-secret"},
            decision_generation=1,
            occurred_at=datetime.now(UTC),
        )
    )
    session.add(
        models.OutboxMessage(
            id=outbox_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="telegram_chat",
            destination_id="provider-target-secret",
            message_type="text",
            payload={
                "text": "secret-payload-text",
                "visibility": "public",
                "target": {"chat_id": "provider-target-secret"},
            },
            reply_to_message_id=inbound_message_id,
            origin_kind="DECISION",
            actor_kind=actor_kind,
            idempotency_key=f"delivery-{outbox_id}",
            status=status,
            attempt_count=attempt_count,
            next_attempt_at=datetime.now(UTC) + timedelta(minutes=5),
            locked_at=datetime.now(UTC),
            locked_by="previous-worker",
            last_error_code=error_code,
            last_error_message="provider diagnostic must stay server-side",
        )
    )
    await session.commit()
    return DeliveryContext(
        tenant_id=tenant_id,
        account_id=account_id,
        conversation_id=conversation_id,
        outbox_id=outbox_id,
    )


def _install_dispatch_spy(monkeypatch) -> list[str]:
    dispatched: list[str] = []

    async def dispatch(_actor, outbox_id: str) -> None:
        dispatched.append(outbox_id)

    monkeypatch.setattr(recovery, "dispatch_actor", dispatch)
    return dispatched


async def test_retry_failed_outbox_is_fenced_audited_dispatched_and_idempotent(
    session,
    monkeypatch,
    recovery_principal,
) -> None:
    context = await _seed_delivery(session, status="FAILED", attempt_count=3)
    dispatched = _install_dispatch_spy(monkeypatch)

    result = await recovery.retry_failed_outbox(
        outbox_id=context.outbox_id,
        required_tenant_id="default",
        actor="user:delivery-admin",
        principal=recovery_principal,
        expected_status="FAILED",
        expected_attempt_count=3,
        review_reason="Provider dashboard confirms the request was rejected.",
        verification_source="PROVIDER_DASHBOARD",
    )
    replay = await recovery.retry_failed_outbox(
        outbox_id=context.outbox_id,
        required_tenant_id="default",
        actor="user:delivery-admin",
        principal=recovery_principal,
        expected_status="FAILED",
        expected_attempt_count=3,
        review_reason="Provider dashboard confirms the request was rejected.",
        verification_source="PROVIDER_DASHBOARD",
    )

    assert result.status == "PENDING"
    assert result.resolution == "CONFIRMED_FAILURE_RETRY"
    assert result.idempotent is False
    assert result.dispatched is True
    assert replay.status == "PENDING"
    assert replay.idempotent is True
    assert replay.dispatched is False
    assert dispatched == [str(context.outbox_id)]

    session.expire_all()
    outbox = await session.get(models.OutboxMessage, context.outbox_id)
    assert outbox is not None
    assert outbox.status == "PENDING"
    assert outbox.attempt_count == 3
    assert outbox.next_attempt_at is None
    assert outbox.locked_at is None
    assert outbox.locked_by is None
    assert outbox.last_error_code == "PROVIDER_REJECTED"
    audit_rows = list(
        await session.scalars(
            select(models.AuditLog).where(
                models.AuditLog.action == "RETRY_CONFIRMED_FAILURE",
                models.AuditLog.subject_id == str(context.outbox_id),
            )
        )
    )
    attempt_rows = list(
        await session.scalars(
            select(models.DeliveryAttempt).where(
                models.DeliveryAttempt.outbox_id == context.outbox_id,
                models.DeliveryAttempt.outcome == "RETRY_CONFIRMED_FAILURE",
            )
        )
    )
    assert len(audit_rows) == 1
    assert audit_rows[0].detail["expected_attempt_count"] == 3
    assert audit_rows[0].detail["verification_source"] == "PROVIDER_DASHBOARD"
    assert len(attempt_rows) == 1
    assert attempt_rows[0].attempt_no == 3


async def test_retry_failed_outbox_hides_scope_and_rejects_status_attempt_and_input_errors(
    session,
    recovery_principal,
) -> None:
    context = await _seed_delivery(session, status="FAILED", attempt_count=4)
    cross_scope = await _seed_delivery(
        session,
        tenant_id="tenant-a",
        status="FAILED",
        attempt_count=1,
    )

    with pytest.raises(recovery.DeliveryRecoveryNotFound) as missing:
        await recovery.retry_failed_outbox(
            outbox_id=cross_scope.outbox_id,
            required_tenant_id="default",
            actor="user:delivery-admin",
            principal=recovery_principal,
            expected_status="FAILED",
            expected_attempt_count=1,
            review_reason="Confirmed failure.",
            verification_source="PROVIDER_DASHBOARD",
        )
    assert missing.value.code == "outbox_not_found"

    with pytest.raises(recovery.DeliveryRecoveryConflict) as attempt_conflict:
        await recovery.retry_failed_outbox(
            outbox_id=context.outbox_id,
            required_tenant_id="default",
            actor="user:delivery-admin",
            principal=recovery_principal,
            expected_status="FAILED",
            expected_attempt_count=3,
            review_reason="Confirmed failure.",
            verification_source="PROVIDER_DASHBOARD",
        )
    assert attempt_conflict.value.code == "delivery_attempt_conflict"

    with pytest.raises(recovery.DeliveryRecoveryConflict) as status_conflict:
        await recovery.retry_failed_outbox(
            outbox_id=context.outbox_id,
            required_tenant_id="default",
            actor="user:delivery-admin",
            principal=recovery_principal,
            expected_status="NEEDS_REVIEW",
            expected_attempt_count=4,
            review_reason="Confirmed failure.",
            verification_source="PROVIDER_DASHBOARD",
        )
    assert status_conflict.value.code == "delivery_status_conflict"

    with pytest.raises(recovery.DeliveryRecoveryValidationError) as invalid_reason:
        await recovery.retry_failed_outbox(
            outbox_id=context.outbox_id,
            required_tenant_id="default",
            actor="user:delivery-admin",
            principal=recovery_principal,
            expected_status="FAILED",
            expected_attempt_count=4,
            review_reason=" ",
            verification_source="PROVIDER_DASHBOARD",
        )
    assert invalid_reason.value.code == "delivery_review_reason_required"

    with pytest.raises(recovery.DeliveryRecoveryValidationError) as reason_too_long:
        await recovery.retry_failed_outbox(
            outbox_id=context.outbox_id,
            required_tenant_id="default",
            actor="user:delivery-admin",
            principal=recovery_principal,
            expected_status="FAILED",
            expected_attempt_count=4,
            review_reason="x" * 501,
            verification_source="PROVIDER_DASHBOARD",
        )
    assert reason_too_long.value.code == "delivery_review_reason_too_long"

    with pytest.raises(recovery.DeliveryRecoveryValidationError) as invalid_source:
        await recovery.retry_failed_outbox(
            outbox_id=context.outbox_id,
            required_tenant_id="default",
            actor="user:delivery-admin",
            principal=recovery_principal,
            expected_status="FAILED",
            expected_attempt_count=4,
            review_reason="Confirmed failure.",
            verification_source="UNTRUSTED_FREE_TEXT",
        )
    assert invalid_source.value.code == "delivery_verification_source_invalid"


async def test_retry_failed_outbox_does_not_overwrite_worker_claim_and_dispatch_failure_is_durable(
    session,
    monkeypatch,
    recovery_principal,
) -> None:
    claimed = await _seed_delivery(session, status="SENDING", attempt_count=5)
    with pytest.raises(recovery.DeliveryRecoveryConflict) as claimed_conflict:
        await recovery.retry_failed_outbox(
            outbox_id=claimed.outbox_id,
            required_tenant_id="default",
            actor="user:delivery-admin",
            principal=recovery_principal,
            expected_status="FAILED",
            expected_attempt_count=5,
            review_reason="Confirmed failure.",
            verification_source="PROVIDER_DASHBOARD",
        )
    assert claimed_conflict.value.code == "delivery_status_conflict"
    session.expire_all()
    claimed_outbox = await session.get(models.OutboxMessage, claimed.outbox_id)
    assert claimed_outbox is not None
    assert claimed_outbox.status == "SENDING"

    durable = await _seed_delivery(session, status="FAILED", attempt_count=2)

    async def fail_dispatch(_actor, _outbox_id: str) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(recovery, "dispatch_actor", fail_dispatch)
    result = await recovery.retry_failed_outbox(
        outbox_id=durable.outbox_id,
        required_tenant_id="default",
        actor="user:delivery-admin",
        principal=recovery_principal,
        expected_status="FAILED",
        expected_attempt_count=2,
        review_reason="Provider confirms failure.",
        verification_source="PROVIDER_API",
    )

    assert result.status == "PENDING"
    assert result.dispatched is False
    session.expire_all()
    durable_outbox = await session.get(models.OutboxMessage, durable.outbox_id)
    assert durable_outbox is not None
    assert durable_outbox.status == "PENDING"


@pytest.mark.parametrize(
    ("resolution", "expected_status", "expected_action", "should_dispatch"),
    (
        (
            "CONFIRMED_NOT_SENT_RETRY",
            "PENDING",
            "RETRY_VERIFIED_NOT_SENT",
            True,
        ),
        (
            "CANCEL",
            "CANCELLED",
            "CANCEL_DELIVERY_AFTER_REVIEW",
            False,
        ),
    ),
)
async def test_resolve_needs_review_supports_retry_and_cancel(
    session,
    monkeypatch,
    recovery_principal,
    resolution: str,
    expected_status: str,
    expected_action: str,
    should_dispatch: bool,
) -> None:
    context = await _seed_delivery(
        session,
        status="NEEDS_REVIEW",
        attempt_count=3,
        error_code="AMBIGUOUS_SEND",
    )
    dispatched = _install_dispatch_spy(monkeypatch)

    result = await recovery.resolve_needs_review_outbox(
        outbox_id=context.outbox_id,
        required_tenant_id="default",
        actor="user:delivery-admin",
        principal=recovery_principal,
        expected_status="NEEDS_REVIEW",
        expected_attempt_count=3,
        review_reason="Provider outcome verified by an administrator.",
        verification_source="ADMIN_OPERATOR_ATTESTED",
        resolution=resolution,
        provider_message_id=None,
    )

    assert result.status == expected_status
    assert result.resolution == resolution
    assert result.dispatched is should_dispatch
    assert dispatched == ([str(context.outbox_id)] if should_dispatch else [])
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, context.outbox_id)
    assert outbox is not None
    assert outbox.status == expected_status
    audit_count = await session.scalar(
        select(func.count())
        .select_from(models.AuditLog)
        .where(
            models.AuditLog.action == expected_action,
            models.AuditLog.subject_id == str(context.outbox_id),
        )
    )
    assert audit_count == 1


async def test_confirmed_sent_materializes_message_once_without_provider_call_and_fences_replay(
    session,
    monkeypatch,
    recovery_principal,
) -> None:
    context = await _seed_delivery(
        session,
        platform="email",
        status="NEEDS_REVIEW",
        attempt_count=6,
        error_code="AMBIGUOUS_SEND",
    )
    dispatched = _install_dispatch_spy(monkeypatch)
    provider_message_id = "<20260901.104512.abc123@mail.delivery.example>"

    result = await recovery.resolve_needs_review_outbox(
        outbox_id=context.outbox_id,
        required_tenant_id="default",
        actor="user:delivery-admin",
        principal=recovery_principal,
        expected_status="NEEDS_REVIEW",
        expected_attempt_count=6,
        review_reason="Provider dashboard shows a successful send.",
        verification_source="PROVIDER_DASHBOARD",
        resolution="CONFIRMED_SENT",
        provider_message_id=provider_message_id,
    )
    replay = await recovery.resolve_needs_review_outbox(
        outbox_id=context.outbox_id,
        required_tenant_id="default",
        actor="user:delivery-admin",
        principal=recovery_principal,
        expected_status="NEEDS_REVIEW",
        expected_attempt_count=6,
        review_reason="Provider dashboard shows a successful send.",
        verification_source="PROVIDER_DASHBOARD",
        resolution="CONFIRMED_SENT",
        provider_message_id=provider_message_id,
    )

    assert result.status == "SENT"
    assert result.dispatched is False
    assert replay.idempotent is True
    assert dispatched == []
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, context.outbox_id)
    assert outbox is not None
    assert outbox.status == "SENT"
    assert outbox.platform_message_id == provider_message_id
    assert outbox.sent_at is not None
    outbound_messages = list(
        await session.scalars(
            select(models.Message).where(models.Message.source_outbox_id == context.outbox_id)
        )
    )
    assert len(outbound_messages) == 1
    assert outbound_messages[0].text == "secret-payload-text"
    assert outbound_messages[0].platform_message_id == provider_message_id
    automation_state = await session.get(models.AutomationState, context.conversation_id)
    assert automation_state is not None
    assert automation_state.last_bot_message_at == outbox.sent_at
    audit_count = await session.scalar(
        select(func.count())
        .select_from(models.AuditLog)
        .where(
            models.AuditLog.action == "CONFIRM_DELIVERY_SENT",
            models.AuditLog.subject_id == str(context.outbox_id),
        )
    )
    attempt_count = await session.scalar(
        select(func.count())
        .select_from(models.DeliveryAttempt)
        .where(
            models.DeliveryAttempt.outbox_id == context.outbox_id,
            models.DeliveryAttempt.outcome == "CONFIRM_DELIVERY_SENT",
        )
    )
    assert audit_count == 1
    assert attempt_count == 1

    with pytest.raises(recovery.DeliveryRecoveryConflict) as conflict:
        await recovery.resolve_needs_review_outbox(
            outbox_id=context.outbox_id,
            required_tenant_id="default",
            actor="user:delivery-admin",
            principal=recovery_principal,
            expected_status="NEEDS_REVIEW",
            expected_attempt_count=6,
            review_reason="Provider dashboard shows a successful send.",
            verification_source="PROVIDER_DASHBOARD",
            resolution="CANCEL",
            provider_message_id=None,
        )
    assert conflict.value.code == "delivery_resolution_conflict"


async def test_confirmed_sent_accepts_email_sender_message_id_without_angle_brackets(
    session,
    recovery_principal,
) -> None:
    context = await _seed_delivery(
        session,
        platform="email",
        status="NEEDS_REVIEW",
        attempt_count=2,
        error_code="AMBIGUOUS_SEND",
    )
    provider_message_id = "20260901.104512.abc123@mail.delivery.example"

    result = await recovery.resolve_needs_review_outbox(
        outbox_id=context.outbox_id,
        required_tenant_id="default",
        actor="user:delivery-admin",
        principal=recovery_principal,
        expected_status="NEEDS_REVIEW",
        expected_attempt_count=2,
        review_reason="SMTP logs confirm the message was accepted.",
        verification_source="PROVIDER_DASHBOARD",
        resolution="CONFIRMED_SENT",
        provider_message_id=provider_message_id,
    )

    assert result.status == "SENT"
    outbox = await session.get(models.OutboxMessage, context.outbox_id)
    assert outbox is not None
    assert outbox.platform_message_id == provider_message_id


async def test_confirmed_sent_email_rejects_crlf_around_rfc_message_id(
    session, recovery_principal
) -> None:
    context = await _seed_delivery(
        session,
        platform="email",
        status="NEEDS_REVIEW",
        attempt_count=2,
        error_code="AMBIGUOUS_SEND",
    )

    with pytest.raises(recovery.DeliveryRecoveryValidationError) as invalid:
        await recovery.resolve_needs_review_outbox(
            outbox_id=context.outbox_id,
            required_tenant_id="default",
            actor="user:delivery-admin",
            principal=recovery_principal,
            expected_status="NEEDS_REVIEW",
            expected_attempt_count=2,
            review_reason="Provider dashboard shows a successful send.",
            verification_source="PROVIDER_DASHBOARD",
            resolution="CONFIRMED_SENT",
            provider_message_id=("<20260901.104512.abc123@mail.delivery.example>\r\n"),
        )

    assert invalid.value.code == "delivery_provider_message_id_invalid"


@pytest.mark.parametrize(
    ("resolution", "provider_message_id", "expected_code"),
    (
        ("CONFIRMED_SENT", None, "delivery_provider_message_id_required"),
        (
            "CONFIRMED_SENT",
            "unsafe provider id <script>",
            "delivery_provider_message_id_invalid",
        ),
        (
            "CONFIRMED_SENT",
            "provider-message\r\nX-Forged-Header: yes",
            "delivery_provider_message_id_invalid",
        ),
        (
            "CONFIRMED_SENT",
            "<20260901.104512.abc123@mail.delivery.example>\r\n",
            "delivery_provider_message_id_invalid",
        ),
        (
            "CONFIRMED_SENT",
            "provider-message\x1b[31m",
            "delivery_provider_message_id_invalid",
        ),
        (
            "CONFIRMED_SENT",
            "p" * 256,
            "delivery_provider_message_id_invalid",
        ),
        ("UNKNOWN", None, "delivery_resolution_invalid"),
    ),
)
async def test_resolve_needs_review_validates_resolution_and_provider_message_id(
    session,
    recovery_principal,
    resolution: str,
    provider_message_id: str | None,
    expected_code: str,
) -> None:
    context = await _seed_delivery(session, status="NEEDS_REVIEW", attempt_count=1)

    with pytest.raises(recovery.DeliveryRecoveryValidationError) as invalid:
        await recovery.resolve_needs_review_outbox(
            outbox_id=context.outbox_id,
            required_tenant_id="default",
            actor="user:delivery-admin",
            principal=recovery_principal,
            expected_status="NEEDS_REVIEW",
            expected_attempt_count=1,
            review_reason="Verified provider outcome.",
            verification_source="PROVIDER_API",
            resolution=resolution,
            provider_message_id=provider_message_id,
        )
    assert invalid.value.code == expected_code


async def test_tenant_delivery_panel_and_retry_route_are_safe_fenced_and_prg(
    session,
    monkeypatch,
) -> None:
    context = await _seed_delivery(session, status="FAILED", attempt_count=4)
    dispatched = _install_dispatch_spy(monkeypatch)

    async with _client() as client:
        csrf_token = await _login(
            client,
            username="admin",
            password="test-admin-password",
        )
        page = await client.get(f"/app/t/default/inbox?queue=delivery&item_id={context.outbox_id}")
        response = await client.post(
            f"/app/t/default/delivery/{context.outbox_id}/retry",
            data={
                "csrf_token": csrf_token,
                "expected_status": "FAILED",
                "expected_attempt_count": "4",
                "review_reason": "Provider rejected the request.",
                "verification_source": "PROVIDER_DASHBOARD",
            },
        )

    assert page.status_code == 200
    assert f'action="/app/t/default/delivery/{context.outbox_id}/retry"' in page.text
    assert 'name="expected_status" value="FAILED"' in page.text
    assert 'name="expected_attempt_count" value="4"' in page.text
    assert "PROVIDER_REJECTED" in page.text
    for sensitive_value in (
        "provider-secret-token",
        "provider-target-secret",
        "full-customer-body-secret",
        "secret-payload-text",
        "provider diagnostic must stay server-side",
    ):
        assert sensitive_value not in page.text
    assert response.status_code == 303
    assert response.headers["location"] == "/app/t/default/inbox?queue=delivery"
    assert dispatched == [str(context.outbox_id)]


async def test_tenant_needs_review_panel_resolves_cancel_with_prg_without_dispatch(
    session,
    monkeypatch,
) -> None:
    context = await _seed_delivery(
        session,
        status="NEEDS_REVIEW",
        attempt_count=5,
        error_code="AMBIGUOUS_SEND",
    )
    dispatched = _install_dispatch_spy(monkeypatch)

    async with _client() as client:
        csrf_token = await _login(
            client,
            username="admin",
            password="test-admin-password",
        )
        page = await client.get(f"/app/t/default/inbox?queue=delivery&item_id={context.outbox_id}")
        response = await client.post(
            f"/app/t/default/delivery/{context.outbox_id}/resolve",
            data={
                "csrf_token": csrf_token,
                "expected_status": "NEEDS_REVIEW",
                "expected_attempt_count": "5",
                "review_reason": "Administrator confirmed no further send is needed.",
                "verification_source": "ADMIN_OPERATOR_ATTESTED",
                "resolution": "CANCEL",
            },
        )

    assert page.status_code == 200
    for resolution in (
        "CONFIRMED_NOT_SENT_RETRY",
        "CONFIRMED_SENT",
        "CANCEL",
    ):
        assert f'name="resolution" value="{resolution}"' in page.text
    assert page.text.count(f'action="/app/t/default/delivery/{context.outbox_id}/resolve"') == 3
    assert response.status_code == 303
    assert response.headers["location"] == "/app/t/default/inbox?queue=delivery"
    assert dispatched == []
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, context.outbox_id)
    assert outbox is not None
    assert outbox.status == "CANCELLED"


async def test_tenant_delivery_routes_enforce_admin_role_csrf_and_hidden_scope(
    session,
) -> None:
    ordinary_user = await _seed_user(session, username="delivery-scope-user", role="USER")
    default_failed = await _seed_delivery(session, status="FAILED", attempt_count=2)
    default_review = await _seed_delivery(
        session,
        status="NEEDS_REVIEW",
        attempt_count=3,
        error_code="AMBIGUOUS_SEND",
    )
    superadmin_failed = await _seed_delivery(session, status="FAILED", attempt_count=2)
    superadmin_review = await _seed_delivery(
        session,
        status="NEEDS_REVIEW",
        attempt_count=3,
        error_code="AMBIGUOUS_SEND",
    )
    other_tenant_failed = await _seed_delivery(
        session,
        tenant_id="tenant-a",
        status="FAILED",
        attempt_count=2,
    )

    retry_form = {
        "expected_status": "FAILED",
        "expected_attempt_count": "2",
        "review_reason": "Confirmed provider failure.",
        "verification_source": "PROVIDER_DASHBOARD",
    }
    async with _client() as client:
        csrf_token = await _login(client, username=ordinary_user.username)
        user_response = await client.post(
            f"/app/t/default/delivery/{default_failed.outbox_id}/retry",
            data={"csrf_token": csrf_token, **retry_form},
        )
        user_resolve_response = await client.post(
            f"/app/t/default/delivery/{default_review.outbox_id}/resolve",
            data={
                "csrf_token": csrf_token,
                "expected_status": "NEEDS_REVIEW",
                "expected_attempt_count": "3",
                "review_reason": "Verified outcome.",
                "verification_source": "ADMIN_OPERATOR_ATTESTED",
                "resolution": "CANCEL",
            },
        )
    async with _client() as client:
        superadmin_csrf = await _login(
            client,
            username="admin",
            password="test-admin-password",
        )
        superadmin_response = await client.post(
            f"/app/t/default/delivery/{superadmin_failed.outbox_id}/retry",
            data={"csrf_token": superadmin_csrf, **retry_form},
        )
        superadmin_resolve_response = await client.post(
            f"/app/t/default/delivery/{superadmin_review.outbox_id}/resolve",
            data={
                "csrf_token": superadmin_csrf,
                "expected_status": "NEEDS_REVIEW",
                "expected_attempt_count": "3",
                "review_reason": "Verified outcome.",
                "verification_source": "ADMIN_OPERATOR_ATTESTED",
                "resolution": "CANCEL",
            },
        )
    async with _client() as client:
        csrf_token = await _login(
            client,
            username="admin",
            password="test-admin-password",
        )
        csrf_response = await client.post(
            f"/app/t/default/delivery/{default_failed.outbox_id}/retry",
            data={"csrf_token": "wrong-token", **retry_form},
        )
        cross_scope_response = await client.post(
            f"/app/t/default/delivery/{other_tenant_failed.outbox_id}/retry",
            data={"csrf_token": csrf_token, **retry_form},
        )
        resolve_csrf_response = await client.post(
            f"/app/t/default/delivery/{default_review.outbox_id}/resolve",
            data={
                "csrf_token": "wrong-token",
                "expected_status": "NEEDS_REVIEW",
                "expected_attempt_count": "3",
                "review_reason": "Verified outcome.",
                "verification_source": "ADMIN_OPERATOR_ATTESTED",
                "resolution": "CANCEL",
            },
        )
        resolve_cross_scope_response = await client.post(
            f"/app/t/default/delivery/{other_tenant_failed.outbox_id}/resolve",
            data={
                "csrf_token": csrf_token,
                "expected_status": "NEEDS_REVIEW",
                "expected_attempt_count": "2",
                "review_reason": "Verified outcome.",
                "verification_source": "ADMIN_OPERATOR_ATTESTED",
                "resolution": "CANCEL",
            },
        )
        ambiguous_retry = await client.post(
            f"/app/t/default/delivery/{default_review.outbox_id}/retry",
            data={
                "csrf_token": csrf_token,
                "expected_status": "NEEDS_REVIEW",
                "expected_attempt_count": "3",
                "review_reason": "Do not blindly retry an ambiguous send.",
                "verification_source": "ADMIN_OPERATOR_ATTESTED",
            },
        )

    assert user_response.status_code == 403
    assert user_response.json() == {"detail": "tenant_admin_required"}
    assert user_resolve_response.status_code == 403
    assert user_resolve_response.json() == {"detail": "tenant_admin_required"}
    assert superadmin_response.status_code == 303
    assert superadmin_resolve_response.status_code == 303
    assert csrf_response.status_code == 403
    assert csrf_response.json() == {"detail": "invalid_csrf_token"}
    assert resolve_csrf_response.status_code == 403
    assert resolve_csrf_response.json() == {"detail": "invalid_csrf_token"}
    assert cross_scope_response.status_code == 404
    assert cross_scope_response.json() == {"detail": "outbox_not_found"}
    assert resolve_cross_scope_response.status_code == 404
    assert resolve_cross_scope_response.json() == {"detail": "outbox_not_found"}
    assert ambiguous_retry.status_code == 409
    assert ambiguous_retry.json() == {"detail": "delivery_status_conflict"}
