from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from social_reply.application.account_management.auth import (
    Principal,
    authenticate,
    hash_password,
)
from social_reply.application.handoff_notifications.callbacks import (
    VerifiedFeishuCallback,
    callback_request_digest,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle
from social_reply.shared.config import get_settings

COMPANY_PASSWORD = "company-integration-password-123"


def bootstrap_password() -> str:
    return get_settings().admin_password.get_secret_value()


FEISHU_APP_ID = "cli-company-integration"
FEISHU_VERIFICATION_TOKEN = "company-verification-token"
FEISHU_ENCRYPT_KEY = "company-encrypt-key"


@dataclass(frozen=True)
class StaffIdentity:
    user_id: uuid.UUID
    username: str
    password: str
    principal: Principal
    raw_token: str


@dataclass(frozen=True)
class ConversationSeed:
    tenant_id: str
    account_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    work_id: uuid.UUID | None


@dataclass(frozen=True)
class FeishuConversationSeed:
    customer_account_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    work_id: uuid.UUID
    intent_id: uuid.UUID
    notification_public_id: uuid.UUID
    action_nonce: uuid.UUID
    provider_message_id: str


@dataclass(frozen=True)
class FeishuHandoffSeed:
    tenant_id: str
    notification_account_id: uuid.UUID
    config_id: uuid.UUID
    app_id: str
    verification_token: str
    encrypt_key: str
    conversations: tuple[FeishuConversationSeed, ...]
    operator_open_ids: tuple[str, ...]


@dataclass(frozen=True)
class SignedFeishuRequest:
    payload: dict[str, Any]
    body: bytes
    headers: dict[str, str]
    proof: VerifiedFeishuCallback
    event_id: str


async def create_staff(
    session,
    *,
    username: str | None = None,
    tenant_id: str = "default",
    role: str = "USER",
    status: str = "active",
    must_change_password: bool = False,
    password: str = COMPANY_PASSWORD,
) -> StaffIdentity:
    username = username or f"company-staff-{uuid.uuid4().hex}"
    user_id = uuid.uuid4()
    user = models.AdminUser(
        id=user_id,
        username=username,
        password_hash=await hash_password(password),
        tenant_id=tenant_id,
        role=role,
        status=status,
        must_change_password=must_change_password,
    )
    session.add(user)
    await session.commit()

    result = await authenticate(username, password)
    if result is None:
        raise AssertionError(f"fixture login failed for {username}")
    principal, raw_token = result
    return StaffIdentity(user_id, username, password, principal, raw_token)


async def bootstrap_identity() -> tuple[Principal, str]:
    settings = get_settings()
    result = await authenticate(settings.admin_username, bootstrap_password())
    if result is None:
        raise AssertionError("fixture bootstrap login failed")
    principal, raw_token = result
    if not principal.is_superadmin:
        raise AssertionError("fixture bootstrap principal is not superadmin")
    return principal, raw_token


async def login_client(
    client: httpx.AsyncClient,
    *,
    username: str,
    password: str,
) -> str:
    login_page = await client.get("/auth/login")
    if login_page.status_code != 200:
        raise AssertionError(f"login page failed: {login_page.status_code}")
    csrf = client.cookies.get("reply_admin_csrf")
    if not isinstance(csrf, str):
        raise AssertionError("login CSRF cookie missing")
    response = await client.post(
        "/auth/login",
        data={"csrf_token": csrf, "username": username, "password": password},
    )
    if response.status_code != 303:
        raise AssertionError(f"login failed: {response.status_code} {response.text}")
    return csrf


async def seed_conversation(
    session,
    *,
    tenant_id: str = "default",
    platform: str = "telegram",
    brand_id: str = "company-brand",
    owner_user_id: uuid.UUID | None = None,
    shared_with_support: bool = True,
    account_status: str = "active",
    account_name: str = "Company support account",
    automation_default: str = "BOT_ACTIVE",
    state: str | None = None,
    work_status: str | None = None,
    assigned_user_id: uuid.UUID | None = None,
    assigned_actor: str | None = None,
    assigned_session_id: uuid.UUID | None = None,
    work_version: int = 1,
) -> ConversationSeed:
    account_id, conversation_id, message_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    work_id = uuid.uuid4() if work_status is not None else None
    resolved_state = state or (
        "HUMAN_ACTIVE"
        if work_status == "CLAIMED"
        else "HANDOFF_PENDING"
        if work_status == "WAITING"
        else "BOT_ACTIVE"
    )
    external_account_id = f"{platform}-{account_id}"
    session.add(
        models.PlatformAccount(
            id=account_id,
            tenant_id=tenant_id,
            brand_id=brand_id,
            platform=platform,
            owner_user_id=owner_user_id,
            shared_with_support=shared_with_support,
            name=account_name,
            external_account_id=external_account_id,
            public_id=f"company-{account_id}",
            credential_bundle={"access_token": "fixture-access-token"},
            config={"delivery_mode": "direct"},
            capability={"dm": True, "max_text_length": 4000},
            automation_default=automation_default,
            status=account_status,
        )
    )
    await session.flush()
    contact_id = uuid.uuid4()
    session.add(
        models.Contact(
            id=contact_id,
            tenant_id=tenant_id,
            platform=platform,
            platform_account_id=account_id,
            external_user_id=f"contact-{conversation_id}",
            display_name="Company customer",
        )
    )
    await session.flush()
    session.add(
        models.Conversation(
            id=conversation_id,
            tenant_id=tenant_id,
            brand_id=brand_id,
            platform=platform,
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"{platform}:{conversation_id}",
            channel_type="dm",
        )
    )
    await session.flush()
    session.add(
        models.AutomationState(
            conversation_id=conversation_id,
            state=resolved_state,
            state_version=1,
            human_agent_id=assigned_actor if work_status == "CLAIMED" else None,
        )
    )
    session.add(
        models.Message(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="Please connect me with a person",
            reply_target={"chat_id": f"chat-{conversation_id}"},
            occurred_at=datetime.now(UTC),
        )
    )
    if work_id is not None:
        claimed_at = datetime.now(UTC) if work_status == "CLAIMED" else None
        session.add(
            models.HumanWorkItem(
                id=work_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                status=work_status,
                reason_code="COMPANY_SUPPORT",
                assigned_user_id=assigned_user_id,
                assigned_actor=assigned_actor,
                assigned_session_id=assigned_session_id,
                claimed_at=claimed_at,
                version=work_version,
            )
        )
    await session.commit()
    return ConversationSeed(tenant_id, account_id, conversation_id, message_id, work_id)


async def seed_feishu_handoff(
    session,
    *,
    staff: tuple[StaffIdentity, ...],
    tenant_id: str = "default",
    conversation_count: int = 1,
    work_status: str = "WAITING",
    assigned_user_ids: tuple[uuid.UUID | None, ...] | None = None,
    customer_shared: bool = True,
    app_id: str = FEISHU_APP_ID,
    verification_token: str = FEISHU_VERIFICATION_TOKEN,
    encrypt_key: str = FEISHU_ENCRYPT_KEY,
) -> FeishuHandoffSeed:
    if conversation_count < 1:
        raise ValueError("conversation_count must be positive")
    if assigned_user_ids is not None and len(assigned_user_ids) != conversation_count:
        raise ValueError("assigned_user_ids length mismatch")
    if not staff:
        raise ValueError("at least one real staff user is required")

    notification_account_id = uuid.uuid4()
    config_id = uuid.uuid4()
    session.add(
        models.PlatformAccount(
            id=notification_account_id,
            tenant_id=tenant_id,
            brand_id="company-notifications",
            platform="feishu",
            name="Company Feishu handoff bot",
            external_account_id=app_id,
            public_id=f"company-feishu-{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle(
                {"app_id": app_id, "app_secret": "fixture-app-secret"}
            ),
            webhook_secret_bundle=encrypt_secret_bundle(
                {"verification_token": verification_token, "encrypt_key": encrypt_key}
            ),
            config={
                "delivery_mode": "direct",
                "feishu_group_mode": "mentions_only",
                "feishu_bot_open_id": "ou_company_bot",
                "feishu_health_status": "READY",
            },
            capability={"dm": True, "mentions": True, "max_text_length": 4000},
            status="active",
        )
    )
    await session.flush()
    session.add(
        models.TenantFeishuHandoffConfig(
            id=config_id,
            tenant_id=tenant_id,
            feishu_platform_account_id=notification_account_id,
            destination_chat_id="oc-company-support",
            enabled=True,
            config_version=1,
        )
    )

    conversations: list[FeishuConversationSeed] = []
    for index in range(conversation_count):
        assigned_user_id = (
            assigned_user_ids[index]
            if assigned_user_ids is not None
            else staff[index % len(staff)].user_id
            if work_status == "CLAIMED"
            else None
        )
        assigned_staff = next(
            (candidate for candidate in staff if candidate.user_id == assigned_user_id), None
        )
        assigned_actor = f"user:{assigned_staff.username}" if assigned_staff else None
        customer = await seed_conversation(
            session,
            tenant_id=tenant_id,
            platform="telegram",
            brand_id=f"company-customer-{index}",
            owner_user_id=None,
            shared_with_support=customer_shared,
            account_name=f"Company customer account {index}",
            state="HUMAN_ACTIVE" if work_status == "CLAIMED" else "HANDOFF_PENDING",
            work_status=work_status,
            assigned_user_id=assigned_user_id,
            assigned_actor=assigned_actor,
            work_version=1,
        )
        work_id = customer.work_id
        if work_id is None:
            raise AssertionError("Feishu fixture requires a work item")
        intent_id = uuid.uuid4()
        notification_public_id = uuid.uuid4()
        action_nonce = uuid.uuid4()
        provider_message_id = f"om-company-card-{index}"
        session.add(
            models.HandoffNotificationIntent(
                id=intent_id,
                public_id=notification_public_id,
                tenant_id=tenant_id,
                human_work_item_id=work_id,
                conversation_id=customer.conversation_id,
                notification_config_id=config_id,
                config_version=1,
                feishu_platform_account_id=notification_account_id,
                destination_chat_id="oc-company-support",
                provider_uuid=uuid.uuid4(),
                provider_message_id=provider_message_id,
                status="SYNCED",
                desired_card_state=work_status,
                desired_revision=1,
                delivered_revision=1,
                action_nonce=action_nonce,
                attempt_count=1,
            )
        )
        conversations.append(
            FeishuConversationSeed(
                customer.account_id,
                customer.conversation_id,
                customer.message_id,
                work_id,
                intent_id,
                notification_public_id,
                action_nonce,
                provider_message_id,
            )
        )

    operator_open_ids: list[str] = []
    for index, identity in enumerate(staff):
        open_id = f"ou-company-operator-{index}-{uuid.uuid4().hex}"
        operator_open_ids.append(open_id)
        session.add(
            models.FeishuHandoffOperator(
                tenant_id=tenant_id,
                feishu_platform_account_id=notification_account_id,
                operator_open_id=open_id,
                display_name=identity.username,
                admin_user_id=identity.user_id,
                can_claim=True,
                can_resolve=True,
                status="ACTIVE",
            )
        )
    await session.commit()
    return FeishuHandoffSeed(
        tenant_id,
        notification_account_id,
        config_id,
        app_id,
        verification_token,
        encrypt_key,
        tuple(conversations),
        tuple(operator_open_ids),
    )


def signed_feishu_body(
    payload: dict[str, Any],
    *,
    encrypt_key: str,
    encrypted: bool = False,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    timestamp = timestamp or str(int(time.time()))
    nonce = nonce or f"company-nonce-{uuid.uuid4().hex}"
    if encrypted:
        plaintext = json.dumps(payload, separators=(",", ":")).encode()
        padding_length = 16 - len(plaintext) % 16
        padded = plaintext + bytes([padding_length]) * padding_length
        iv = bytes(range(16))
        key = hashlib.sha256(encrypt_key.encode()).digest()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        encrypted_payload = base64.b64encode(
            iv + encryptor.update(padded) + encryptor.finalize()
        ).decode()
        body = json.dumps({"encrypt": encrypted_payload}, separators=(",", ":")).encode()
    else:
        body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hashlib.sha256(
        timestamp.encode() + nonce.encode() + encrypt_key.encode() + body
    ).hexdigest()
    return body, {
        "Content-Type": "application/json",
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": signature,
    }


def signed_card_request(
    seed: FeishuHandoffSeed,
    conversation: FeishuConversationSeed,
    *,
    operator_open_id: str,
    action: str,
    expected_work_version: int,
    expected_card_revision: int,
    action_nonce: uuid.UUID,
    event_id: str | None = None,
    encrypted: bool = False,
) -> SignedFeishuRequest:
    event_id = event_id or f"company-card-event-{uuid.uuid4().hex}"
    event = {
        "operator": {"open_id": operator_open_id},
        "open_message_id": conversation.provider_message_id,
        "action": {
            "value": {
                "contract_version": 1,
                "notification_id": str(conversation.notification_public_id),
                "action": action,
                "expected_work_version": expected_work_version,
                "expected_card_revision": expected_card_revision,
                "action_nonce": str(action_nonce),
            }
        },
    }
    payload = {
        "schema": "2.0",
        "header": {
            "event_type": "card.action.trigger",
            "event_id": event_id,
            "token": seed.verification_token,
            "app_id": seed.app_id,
        },
        "event": event,
    }
    body, headers = signed_feishu_body(
        payload,
        encrypt_key=seed.encrypt_key,
        encrypted=encrypted,
    )
    proof = callback_request_digest(
        body,
        account_id=seed.notification_account_id,
        tenant_id=seed.tenant_id,
        app_id=seed.app_id,
        verification_token=seed.verification_token,
        encrypt_key=seed.encrypt_key,
        timestamp=headers["X-Lark-Request-Timestamp"],
        nonce=headers["X-Lark-Request-Nonce"],
        signature=headers["X-Lark-Signature"],
    )
    return SignedFeishuRequest(payload, body, headers, proof, event_id)
