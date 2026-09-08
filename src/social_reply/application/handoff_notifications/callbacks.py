import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from social_reply.application.account_management.access import (
    lock_session_authorities,
    lock_user_authority,
    user_can_access_account,
)
from social_reply.application.account_management.auth import FeishuActionProof, Principal
from social_reply.application.account_management.human_workflow import (
    HumanWorkflowConflict,
    HumanWorkflowError,
    claim_human_work_item_in_session,
    resolve_human_work_item_in_session,
)
from social_reply.application.handoff_notifications.projection import (
    render_current_handoff_card,
)
from social_reply.application.handoff_notifications.service import HandoffNotificationError
from social_reply.connectors.feishu.security import (
    FeishuSecurityError,
    decrypt_payload,
    parse_json_object,
    verify_signature,
    verify_token,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_conversation_delivery_xact_lock,
)
from social_reply.infrastructure.database.engine import get_session_factory


class FeishuCardActionError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedCardAction:
    notification_public_id: uuid.UUID
    action: str
    expected_work_version: int
    expected_card_revision: int
    action_nonce: uuid.UUID
    operator_open_id: str
    open_message_id: str


_FEISHU_CALLBACK_MARKER = object()


@dataclass(frozen=True)
class VerifiedFeishuCallback:
    """Opaque digest produced after the Feishu router has verified the callback."""

    digest: str
    account_id: uuid.UUID | None = None
    tenant_id: str = ""
    provider_event_id: str = ""
    event_digest: str = ""
    _marker: object | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def is_verified(self) -> bool:
        return self._marker is _FEISHU_CALLBACK_MARKER


def _event_digest(event: object) -> str:
    encoded = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def callback_request_digest(
    body: bytes,
    *,
    account_id: uuid.UUID,
    tenant_id: str,
    app_id: str,
    verification_token: str,
    encrypt_key: str,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
) -> VerifiedFeishuCallback:
    if not verification_token.strip() or not encrypt_key.strip():
        raise FeishuSecurityError()
    verify_signature(
        timestamp=timestamp, nonce=nonce, signature=signature, encrypt_key=encrypt_key, body=body
    )
    envelope = parse_json_object(body)
    encrypted = envelope.get("encrypt")
    payload = (
        decrypt_payload(encrypted, encrypt_key=encrypt_key)
        if isinstance(encrypted, str)
        else envelope
    )
    header = payload.get("header")
    if not isinstance(header, dict) or header.get("event_type") != "card.action.trigger":
        raise FeishuSecurityError()
    try:
        verify_token(header.get("token"), expected=verification_token)
    except FeishuSecurityError:
        verify_token(payload.get("token"), expected=verification_token)
    received_app_id = header.get("app_id")
    event_id = header.get("event_id")
    if (
        not isinstance(received_app_id, str)
        or not secrets.compare_digest(received_app_id, app_id)
        or not isinstance(event_id, str)
        or not event_id.strip()
    ):
        raise FeishuSecurityError()
    callback = VerifiedFeishuCallback(
        digest=hashlib.sha256(body).hexdigest(),
        account_id=account_id,
        tenant_id=tenant_id,
        provider_event_id=event_id,
        event_digest=_event_digest(payload.get("event")),
    )
    object.__setattr__(callback, "_marker", _FEISHU_CALLBACK_MARKER)
    return callback


def parse_card_action(event: object) -> ParsedCardAction:
    if not isinstance(event, dict):
        raise FeishuCardActionError("feishu_card_event_invalid")
    operator = event.get("operator")
    action_data = event.get("action")
    context = event.get("context")
    context_message_id = context.get("open_message_id") if isinstance(context, dict) else None
    open_message_id = context_message_id or event.get("open_message_id")
    if not isinstance(operator, dict) or not isinstance(action_data, dict):
        raise FeishuCardActionError("feishu_card_event_invalid")
    operator_open_id = operator.get("open_id")
    value = action_data.get("value")
    # Feishu delivers the interactive button value back as either an object or a
    # JSON-encoded string depending on how the card was authored.
    if isinstance(value, str):
        try:
            parsed_value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise FeishuCardActionError("feishu_card_event_invalid") from exc
        value = parsed_value
    if (
        not isinstance(operator_open_id, str)
        or not operator_open_id.strip()
        or not isinstance(open_message_id, str)
        or not open_message_id.strip()
        or not isinstance(value, dict)
    ):
        raise FeishuCardActionError("feishu_card_event_invalid")
    if value.get("contract_version") != 1:
        raise FeishuCardActionError("feishu_card_contract_version_invalid")
    action = value.get("action")
    work_version = value.get("expected_work_version")
    card_revision = value.get("expected_card_revision")
    if action not in {"claim", "resolve"}:
        raise FeishuCardActionError("feishu_card_action_invalid")
    if type(work_version) is not int or type(card_revision) is not int:
        raise FeishuCardActionError("feishu_card_action_version_invalid")
    if work_version < 1 or card_revision < 1:
        raise FeishuCardActionError("feishu_card_action_version_invalid")
    try:
        notification_id = uuid.UUID(str(value.get("notification_id") or ""))
        action_nonce = uuid.UUID(str(value.get("action_nonce") or ""))
    except ValueError as exc:
        raise FeishuCardActionError("feishu_card_action_identity_invalid") from exc
    return ParsedCardAction(
        notification_public_id=notification_id,
        action=action,
        expected_work_version=work_version,
        expected_card_revision=card_revision,
        action_nonce=action_nonce,
        operator_open_id=operator_open_id.strip(),
        open_message_id=open_message_id.strip(),
    )


def _response(
    toast_type: str,
    content: str,
    *,
    card: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "toast": {"type": toast_type, "content": content},
    }
    if card is not None:
        payload["card"] = {"type": "raw", "data": card}
    return payload


async def _render_intent_card(
    session,
    intent: models.HandoffNotificationIntent,
) -> dict[str, object]:
    conversation = await session.get(models.Conversation, intent.conversation_id)
    work = await session.get(models.HumanWorkItem, intent.human_work_item_id)
    state = await session.get(models.AutomationState, intent.conversation_id)
    if conversation is None or work is None or state is None:
        raise FeishuCardActionError("feishu_card_resource_missing")
    return await render_current_handoff_card(
        session,
        intent=intent,
        conversation=conversation,
        work=work,
        state=state,
    )


async def _existing_receipt_response(
    session,
    *,
    account_id: uuid.UUID,
    provider_event_id: str,
    request_digest: str,
) -> dict[str, object] | None:
    receipt = await session.scalar(
        select(models.FeishuCardActionReceipt)
        .where(
            models.FeishuCardActionReceipt.feishu_platform_account_id == account_id,
            models.FeishuCardActionReceipt.provider_event_id == provider_event_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if receipt is None:
        return None
    if receipt.request_digest != request_digest:
        return _response("error", "回调标识冲突，操作未执行")
    if receipt.response_payload:
        return dict(receipt.response_payload)
    return _response("warning", "操作正在处理中，请稍后重试")


async def _lock_callback_participants(
    session, *, account_id, tenant_id, parsed, intent, provider_event_id, request_digest
):
    operator_query = select(models.FeishuHandoffOperator.admin_user_id).where(
        models.FeishuHandoffOperator.tenant_id == tenant_id,
        models.FeishuHandoffOperator.feishu_platform_account_id == account_id,
        models.FeishuHandoffOperator.operator_open_id == parsed.operator_open_id,
    )
    actor_id = await session.scalar(operator_query)
    expected_work_id = intent.human_work_item_id if intent is not None else None
    work_query = None
    work = None
    if intent is not None and intent.tenant_id == tenant_id:
        work_query = select(
            models.HumanWorkItem.conversation_id,
            models.HumanWorkItem.assigned_user_id,
            models.HumanWorkItem.assigned_session_id,
        ).where(
            models.HumanWorkItem.id == intent.human_work_item_id,
            models.HumanWorkItem.tenant_id == tenant_id,
        )
        work = (await session.execute(work_query)).one_or_none()
        if work is not None and work.conversation_id != intent.conversation_id:
            raise FeishuCardActionError("feishu_card_work_scope_mismatch")
    account_ids = {account_id}
    source_account_id = None
    if work is not None:
        source_account_id = await session.scalar(
            select(models.Conversation.platform_account_id).where(
                models.Conversation.id == work.conversation_id,
                models.Conversation.tenant_id == tenant_id,
            )
        )
        if source_account_id is None:
            raise FeishuCardActionError("feishu_card_work_scope_mismatch")
        account_ids.add(source_account_id)
    staff_ids = {actor_id} if actor_id is not None else set()
    session_ids = set()
    if work is not None:
        if work.assigned_user_id is not None:
            staff_ids.add(work.assigned_user_id)
        if work.assigned_session_id is not None:
            session_ids.add(work.assigned_session_id)
    if session_ids:
        staff_ids.update(
            value
            for value in await session.scalars(
                select(models.AdminSession.user_id).where(models.AdminSession.id.in_(session_ids))
            )
            if value is not None
        )
    for user_id in sorted(staff_ids, key=str):
        await lock_user_authority(session, user_id)
    await lock_session_authorities(session, session_ids)
    if session_ids:
        await session.execute(
            select(models.AdminSession.id)
            .where(models.AdminSession.id.in_(session_ids))
            .order_by(models.AdminSession.id)
            .with_for_update()
        )
    if work is not None:
        await acquire_conversation_delivery_xact_lock(session, work.conversation_id)
    cached = await _existing_receipt_response(
        session,
        account_id=account_id,
        provider_event_id=provider_event_id,
        request_digest=request_digest,
    )
    if cached is not None:
        return cached
    if await session.scalar(operator_query) != actor_id:
        raise FeishuCardActionError("feishu_card_participants_changed")
    if work_query is not None:
        current_work = (await session.execute(work_query)).one_or_none()
        if (work is None) != (current_work is None):
            raise FeishuCardActionError("feishu_card_participants_changed")
        if current_work is not None and (
            current_work.conversation_id != work.conversation_id
            or (
                current_work.assigned_user_id is not None
                and current_work.assigned_user_id not in staff_ids
            )
            or (
                current_work.assigned_session_id is not None
                and current_work.assigned_session_id not in session_ids
            )
        ):
            raise FeishuCardActionError("feishu_card_participants_changed")
    locked_accounts = set(
        await session.scalars(
            select(models.PlatformAccount.id)
            .where(
                models.PlatformAccount.tenant_id == tenant_id,
                models.PlatformAccount.id.in_(account_ids),
            )
            .order_by(models.PlatformAccount.id)
            .with_for_update()
        )
    )
    if locked_accounts != account_ids:
        raise FeishuCardActionError("feishu_card_account_scope_mismatch")
    if work is not None:
        current_source = await session.scalar(
            select(models.Conversation.platform_account_id).where(
                models.Conversation.id == work.conversation_id,
                models.Conversation.tenant_id == tenant_id,
            )
        )
        if current_source != source_account_id:
            raise FeishuCardActionError("feishu_card_work_scope_mismatch")
    if intent is not None:
        await session.refresh(intent)
        if intent.human_work_item_id != expected_work_id:
            raise FeishuCardActionError("feishu_card_work_scope_mismatch")
        if work is not None and intent.conversation_id != work.conversation_id:
            raise FeishuCardActionError("feishu_card_work_scope_mismatch")
    return None


async def handle_feishu_card_action(
    *,
    account_id: uuid.UUID,
    tenant_id: str,
    provider_event_id: str,
    request_digest: VerifiedFeishuCallback,
    event: object,
    feature_enabled: bool,
) -> dict[str, object]:
    if not isinstance(request_digest, VerifiedFeishuCallback) or not request_digest.is_verified:
        raise FeishuCardActionError("feishu_callback_verification_required")
    if (
        request_digest.account_id != account_id
        or request_digest.tenant_id != tenant_id
        or request_digest.provider_event_id != provider_event_id
        or not secrets.compare_digest(request_digest.event_digest, _event_digest(event))
    ):
        raise FeishuCardActionError("feishu_callback_scope_mismatch")
    parsed = parse_card_action(event)
    request_digest = request_digest.digest
    if len(request_digest) != 64 or any(
        character not in "0123456789abcdef" for character in request_digest
    ):
        raise FeishuCardActionError("feishu_card_request_digest_invalid")
    async with get_session_factory()() as session:
        notification_account = await session.scalar(
            select(models.PlatformAccount)
            .where(
                models.PlatformAccount.id == account_id,
                models.PlatformAccount.tenant_id == tenant_id,
                models.PlatformAccount.platform == "feishu",
                models.PlatformAccount.status == "active",
            )
            .execution_options(populate_existing=True)
        )
        if notification_account is None:
            raise FeishuCardActionError("feishu_card_account_scope_mismatch")
        intent = await session.scalar(
            select(models.HandoffNotificationIntent).where(
                models.HandoffNotificationIntent.public_id == parsed.notification_public_id,
                models.HandoffNotificationIntent.tenant_id == tenant_id,
                models.HandoffNotificationIntent.feishu_platform_account_id == account_id,
            )
        )
        intent_id = intent.id if intent is not None and intent.tenant_id == tenant_id else None
        cached = await _lock_callback_participants(
            session,
            account_id=account_id,
            tenant_id=tenant_id,
            parsed=parsed,
            intent=intent,
            provider_event_id=provider_event_id,
            request_digest=request_digest,
        )
        if cached is not None:
            await session.commit()
            return cached
        receipt_id = uuid.uuid4()
        inserted_id = (
            await session.execute(
                pg_insert(models.FeishuCardActionReceipt)
                .values(
                    id=receipt_id,
                    tenant_id=tenant_id,
                    feishu_platform_account_id=account_id,
                    provider_event_id=provider_event_id,
                    notification_intent_id=intent_id,
                    operator_open_id=parsed.operator_open_id,
                    action=parsed.action.upper(),
                    request_digest=request_digest,
                    outcome="PROCESSING",
                    response_payload={},
                )
                .on_conflict_do_nothing(
                    index_elements=["feishu_platform_account_id", "provider_event_id"]
                )
                .returning(models.FeishuCardActionReceipt.id)
            )
        ).scalar_one_or_none()
        if inserted_id is None:
            response = await _existing_receipt_response(
                session,
                account_id=account_id,
                provider_event_id=provider_event_id,
                request_digest=request_digest,
            )
            await session.commit()
            return response or _response("warning", "操作正在处理中，请稍后重试")
        receipt = await session.get(models.FeishuCardActionReceipt, inserted_id)
        if receipt is None:
            raise FeishuCardActionError("feishu_card_receipt_missing")
        conversation = None
        customer_account = None
        if intent is not None and intent.tenant_id == tenant_id:
            conversation = await session.scalar(
                select(models.Conversation).where(
                    models.Conversation.id == intent.conversation_id,
                    models.Conversation.tenant_id == tenant_id,
                )
            )
            if conversation is not None:
                customer_account = await session.scalar(
                    select(models.PlatformAccount).where(
                        models.PlatformAccount.id == conversation.platform_account_id,
                        models.PlatformAccount.tenant_id == tenant_id,
                    )
                )
        if (
            intent is None
            or intent.tenant_id != tenant_id
            or intent.feishu_platform_account_id != account_id
            or intent.provider_message_id != parsed.open_message_id
            or conversation is None
            or customer_account is None
            or conversation.platform_account_id != customer_account.id
        ):
            response = _response("error", "卡片与工单不匹配，操作未执行")
            receipt.outcome = "CONFLICT"
            receipt.response_payload = response
            receipt.completed_at = datetime.now(UTC)
            await session.commit()
            return response

        operator = await session.scalar(
            select(models.FeishuHandoffOperator).where(
                models.FeishuHandoffOperator.tenant_id == tenant_id,
                models.FeishuHandoffOperator.feishu_platform_account_id == account_id,
                models.FeishuHandoffOperator.operator_open_id == parsed.operator_open_id,
                models.FeishuHandoffOperator.status == "ACTIVE",
            )
        )
        permission_allowed = operator is not None and (
            (parsed.action == "claim" and operator.can_claim)
            or (parsed.action == "resolve" and operator.can_resolve)
        )
        admin_user = (
            await session.scalar(
                select(models.AdminUser).where(
                    models.AdminUser.id == operator.admin_user_id,
                    models.AdminUser.tenant_id == tenant_id,
                    models.AdminUser.status == "active",
                    models.AdminUser.role.in_(["USER", "WORKSPACE_ADMIN"]),
                )
            )
            if operator is not None and operator.admin_user_id is not None
            else None
        )
        if (
            not permission_allowed
            or admin_user is None
            or not user_can_access_account(admin_user, customer_account)
        ):
            response = _response("error", "你没有该 Tenant 的工单操作权限")
            receipt.outcome = "UNAUTHORIZED"
            receipt.response_payload = response
            receipt.completed_at = datetime.now(UTC)
            await session.commit()
            return response
        if not feature_enabled:
            response = _response("warning", "人工接管卡片功能维护中，请使用 Reply Core")
            receipt.outcome = "MAINTENANCE"
            receipt.response_payload = response
            receipt.completed_at = datetime.now(UTC)
            await session.commit()
            return response

        proof = FeishuActionProof(
            receipt_id=receipt.id,
            tenant_id=tenant_id,
            notification_account_id=account_id,
            notification_intent_id=intent.id,
            customer_account_id=customer_account.id,
            operator_open_id=parsed.operator_open_id,
            action=parsed.action.upper(),
            action_nonce=parsed.action_nonce,
            request_digest=request_digest,
            provider_message_id=parsed.open_message_id,
        )
        principal = Principal(
            session_id=None,
            username=admin_user.username,
            actor=f"user:{admin_user.username}",
            allowed_tenants=frozenset({tenant_id}),
            user_id=admin_user.id,
            tenant_id=tenant_id,
            must_change_password=admin_user.must_change_password,
            role=admin_user.role,
            authentication_kind="FEISHU_ACTION",
            action_proof=proof,
        )
        try:
            if parsed.action == "claim":
                conversation, _account, work, state = await claim_human_work_item_in_session(
                    session,
                    work_item_id=intent.human_work_item_id,
                    allowed_tenants=frozenset({tenant_id}),
                    actor=principal.actor,
                    user_id=principal.user_id,
                    expected_version=parsed.expected_work_version,
                    notification_public_id=parsed.notification_public_id,
                    expected_card_revision=parsed.expected_card_revision,
                    expected_action_nonce=parsed.action_nonce,
                    principal=principal,
                )
                toast = "认领成功"
            else:
                conversation, _account, work, state = await resolve_human_work_item_in_session(
                    session,
                    work_item_id=intent.human_work_item_id,
                    allowed_tenants=frozenset({tenant_id}),
                    actor=principal.actor,
                    user_id=principal.user_id,
                    expected_version=parsed.expected_work_version,
                    allow_override=False,
                    resolution_evidence="FEISHU_OPERATOR_ATTESTED",
                    notification_public_id=parsed.notification_public_id,
                    expected_card_revision=parsed.expected_card_revision,
                    expected_action_nonce=parsed.action_nonce,
                    principal=principal,
                )
                toast = "已恢复该会话的账号自动化策略"
            updated_intent = await session.scalar(
                select(models.HandoffNotificationIntent).where(
                    models.HandoffNotificationIntent.id == intent.id
                )
            )
            if updated_intent is None:
                raise FeishuCardActionError("feishu_card_resource_missing")
            card = await render_current_handoff_card(
                session,
                intent=updated_intent,
                conversation=conversation,
                work=work,
                state=state,
            )
            response = _response("success", toast, card=card)
            receipt.outcome = "SUCCEEDED"
        except (HumanWorkflowConflict, HandoffNotificationError) as exc:
            current_intent = await session.get(
                models.HandoffNotificationIntent,
                intent.id,
            )
            if current_intent is not None:
                await session.refresh(current_intent)
            card = (
                await _render_intent_card(session, current_intent)
                if current_intent is not None
                else None
            )
            message = (
                "回复尚未投递或结果待核实，请先确认投递或取消后再结束处理"
                if str(exc) == "human_reply_delivery_pending"
                else "卡片状态已变化，已显示最新状态"
            )
            response = _response("warning", message, card=card)
            receipt.outcome = "CONFLICT"
        except HumanWorkflowError:
            response = _response("error", "工单当前无法执行该操作")
            receipt.outcome = "CONFLICT"
        receipt.response_payload = response
        receipt.completed_at = datetime.now(UTC)
        await session.commit()
        return response
