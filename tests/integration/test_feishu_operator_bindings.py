import hashlib
import json
import time
import uuid

import pytest
from sqlalchemy import func, select
from tests.integration.test_human_feishu_outbox_contracts import _seed_conversation

from social_reply.application.account_management.auth import authenticate, hash_password
from social_reply.application.account_management.feishu_handoff_service import (
    FeishuHandoffConflict,
    FeishuHandoffValidationError,
    upsert_feishu_handoff_operator,
)
from social_reply.application.account_management.staff_lifecycle import revoke_staff_authority
from social_reply.application.handoff_notifications.callbacks import (
    callback_request_digest,
    handle_feishu_card_action,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

pytestmark = pytest.mark.integration
_PASSWORD = "operator-binding-test-password"
_TOKEN = "operator-verification-token"
_KEY = "operator-encrypt-key"
_APP_ID = "cli-operator-binding"


async def _setup(session):
    password_hash = await hash_password(_PASSWORD)
    admin = models.AdminUser(
        username=f"binding-admin-{uuid.uuid4().hex}",
        password_hash=password_hash,
        tenant_id="default",
        role="WORKSPACE_ADMIN",
        status="active",
        must_change_password=False,
    )
    employee = models.AdminUser(
        username=f"binding-support-{uuid.uuid4().hex}",
        password_hash=password_hash,
        tenant_id="default",
        role="USER",
        status="active",
        must_change_password=False,
    )
    bot = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="feishu",
        name="Notifications",
        external_account_id=_APP_ID,
        public_id=f"bindings-{uuid.uuid4().hex}",
        status="active",
        credential_bundle=encrypt_secret_bundle({"app_id": _APP_ID, "app_secret": "test-secret"}),
        webhook_secret_bundle=encrypt_secret_bundle(
            {"verification_token": _TOKEN, "encrypt_key": _KEY}
        ),
        config={"delivery_mode": "direct", "feishu_health_status": "READY"},
        capability={"dm": True},
        shared_with_support=False,
    )
    session.add_all([admin, employee, bot])
    await session.flush()
    config = models.TenantFeishuHandoffConfig(
        tenant_id="default",
        feishu_platform_account_id=bot.id,
        destination_chat_id="oc_support",
        enabled=True,
    )
    session.add(config)
    await session.commit()
    result = await authenticate(admin.username, _PASSWORD)
    assert result is not None
    return result[0], employee, bot, config


def _command(principal):
    return {
        "tenant_id": "default",
        "actor": "untrusted-actor-text",
        "principal": principal,
        "operator_open_id": "ou_employee_b",
        "display_name": "Employee B",
        "can_claim": True,
        "can_resolve": True,
    }


async def test_operator_creation_needs_explicit_employee_and_edit_preserves_binding(session):
    principal, employee, _bot, _config = await _setup(session)
    command = _command(principal)
    with pytest.raises(FeishuHandoffValidationError, match="feishu_operator_staff_required"):
        await upsert_feishu_handoff_operator(**command)
    assert await session.scalar(select(func.count()).select_from(models.FeishuHandoffOperator)) == 0
    operator_id = await upsert_feishu_handoff_operator(**command, admin_user_id=employee.id)
    await upsert_feishu_handoff_operator(
        **{**command, "display_name": "B renamed", "can_claim": False}
    )
    operator = await session.get(models.FeishuHandoffOperator, operator_id)
    assert operator.admin_user_id == employee.id
    assert operator.display_name == "B renamed"
    assert operator.can_claim is False
    with pytest.raises(FeishuHandoffConflict, match="rebind_confirmation_required"):
        await upsert_feishu_handoff_operator(**command, admin_user_id=principal.user_id)
    await session.refresh(operator)
    assert operator.admin_user_id == employee.id
    await upsert_feishu_handoff_operator(
        **command,
        admin_user_id=principal.user_id,
        confirm_rebind=True,
        expected_admin_user_id=employee.id,
    )
    await session.refresh(operator)
    assert operator.admin_user_id == principal.user_id
    audit = await session.scalar(
        select(models.AuditLog).where(
            models.AuditLog.action == "REBIND_FEISHU_HANDOFF_OPERATOR",
            models.AuditLog.subject_id == str(operator_id),
        )
    )
    assert audit.detail["previous_admin_user_id"] == str(employee.id)
    assert audit.detail["admin_user_id"] == str(principal.user_id)


async def test_configured_employee_not_configuring_admin_owns_feishu_action(session):
    principal, employee, bot, config = await _setup(session)
    employee_id, username, bot_id, config_id = employee.id, employee.username, bot.id, config.id
    operator_id = await upsert_feishu_handoff_operator(
        **_command(principal), admin_user_id=employee_id
    )
    _account_id, conversation_id, _message_id, work_id = await _seed_conversation(
        session,
        tenant_id="default",
        shared_with_support=True,
        state="HANDOFF_PENDING",
        with_work=True,
    )
    intent = models.HandoffNotificationIntent(
        id=uuid.uuid4(),
        public_id=uuid.uuid4(),
        tenant_id="default",
        human_work_item_id=work_id,
        conversation_id=conversation_id,
        notification_config_id=config_id,
        config_version=1,
        feishu_platform_account_id=bot_id,
        destination_chat_id="oc_support",
        provider_uuid=uuid.uuid4(),
        provider_message_id="om_binding",
        status="SYNCED",
        desired_card_state="WAITING",
        desired_revision=1,
        delivered_revision=1,
        action_nonce=uuid.uuid4(),
    )
    session.add(intent)
    await session.commit()
    nonce, public_id = intent.action_nonce, intent.public_id

    async def click(event_id, action, version, revision, action_nonce):
        event = {
            "operator": {"open_id": "ou_employee_b"},
            "open_message_id": "om_binding",
            "action": {
                "value": {
                    "contract_version": 1,
                    "notification_id": str(public_id),
                    "action": action,
                    "expected_work_version": version,
                    "expected_card_revision": revision,
                    "action_nonce": str(action_nonce),
                }
            },
        }
        body = json.dumps(
            {
                "header": {
                    "event_type": "card.action.trigger",
                    "event_id": event_id,
                    "app_id": _APP_ID,
                    "token": _TOKEN,
                },
                "event": event,
            }
        ).encode()
        timestamp, request_nonce = str(int(time.time())), "binding-proof"
        signature = hashlib.sha256((timestamp + request_nonce + _KEY).encode() + body).hexdigest()
        proof = callback_request_digest(
            body,
            account_id=bot_id,
            tenant_id="default",
            app_id=_APP_ID,
            verification_token=_TOKEN,
            encrypt_key=_KEY,
            timestamp=timestamp,
            nonce=request_nonce,
            signature=signature,
        )
        return await handle_feishu_card_action(
            account_id=bot_id,
            tenant_id="default",
            provider_event_id=event_id,
            request_digest=proof,
            event=event,
            feature_enabled=True,
        )

    result = await click("binding-claim", "claim", 1, 1, nonce)
    assert result["toast"]["type"] == "success"
    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_id)
    assert work.assigned_user_id == employee_id
    assert work.assigned_user_id != principal.user_id
    assert work.assigned_actor == f"user:{username}"
    audit = await session.scalar(
        select(models.AuditLog).where(
            models.AuditLog.action == "CLAIM", models.AuditLog.subject_id == str(work_id)
        )
    )
    assert audit.actor == f"user:{username}"
    receipt = await session.scalar(
        select(models.FeishuCardActionReceipt).where(
            models.FeishuCardActionReceipt.provider_event_id == "binding-claim"
        )
    )
    assert receipt.operator_open_id == "ou_employee_b"
    async with get_session_factory()() as revocation:
        await revoke_staff_authority(revocation, user_id=employee_id, reason="STAFF_DISABLED")
        bound = await revocation.get(models.AdminUser, employee_id)
        bound.status = "disabled"
        await revocation.commit()
    await session.refresh(intent)
    denied = await click(
        "binding-after-revoke", "claim", 3, intent.desired_revision, intent.action_nonce
    )
    assert denied["toast"]["type"] == "error"
    operator = await session.get(models.FeishuHandoffOperator, operator_id)
    assert operator.admin_user_id == employee_id
    assert operator.status == "DISABLED"
