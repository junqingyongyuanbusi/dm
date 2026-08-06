import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

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
from social_reply.infrastructure.database import models
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


def callback_request_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def parse_card_action(event: object) -> ParsedCardAction:
    if not isinstance(event, dict):
        raise FeishuCardActionError("feishu_card_event_invalid")
    operator = event.get("operator")
    action_data = event.get("action")
    open_message_id = event.get("open_message_id")
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
        select(models.FeishuCardActionReceipt).where(
            models.FeishuCardActionReceipt.feishu_platform_account_id == account_id,
            models.FeishuCardActionReceipt.provider_event_id == provider_event_id,
        )
    )
    if receipt is None:
        return None
    if receipt.request_digest != request_digest:
        return _response("error", "回调标识冲突，操作未执行")
    if receipt.response_payload:
        return dict(receipt.response_payload)
    return _response("warning", "操作正在处理中，请稍后重试")


async def handle_feishu_card_action(
    *,
    account_id: uuid.UUID,
    tenant_id: str,
    provider_event_id: str,
    request_digest: str,
    event: object,
    feature_enabled: bool,
) -> dict[str, object]:
    parsed = parse_card_action(event)
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
        if account is None or account.tenant_id != tenant_id or account.platform != "feishu":
            raise FeishuCardActionError("feishu_card_account_scope_mismatch")
        intent = await session.scalar(
            select(models.HandoffNotificationIntent).where(
                models.HandoffNotificationIntent.public_id == parsed.notification_public_id
            )
        )
        intent_id = intent.id if intent is not None and intent.tenant_id == tenant_id else None
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
        if (
            intent is None
            or intent.tenant_id != tenant_id
            or intent.feishu_platform_account_id != account_id
            or intent.provider_message_id != parsed.open_message_id
        ):
            response = _response("error", "卡片与工单不匹配，操作未执行")
            receipt.outcome = "CONFLICT"
            receipt.response_payload = response
            receipt.completed_at = datetime.now(UTC)
            await session.commit()
            return response

        operator = await session.scalar(
            select(models.FeishuHandoffOperator)
            .where(
                models.FeishuHandoffOperator.tenant_id == tenant_id,
                models.FeishuHandoffOperator.feishu_platform_account_id == account_id,
                models.FeishuHandoffOperator.operator_open_id == parsed.operator_open_id,
                models.FeishuHandoffOperator.status == "ACTIVE",
            )
            .with_for_update()
        )
        permission_allowed = operator is not None and (
            (parsed.action == "claim" and operator.can_claim)
            or (parsed.action == "resolve" and operator.can_resolve)
        )
        if not permission_allowed:
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

        intent_db_id = intent.id
        actor = f"feishu_operator:{operator.id}"
        user_id = operator.admin_user_id
        if user_id is not None:
            admin_user = await session.get(models.AdminUser, user_id)
            if (
                admin_user is None
                or admin_user.tenant_id != tenant_id
                or admin_user.status != "active"
            ):
                response = _response("error", "关联的管理员账号不可用")
                receipt.outcome = "UNAUTHORIZED"
                receipt.response_payload = response
                receipt.completed_at = datetime.now(UTC)
                await session.commit()
                return response
            actor = f"user:{admin_user.username}"

        try:
            if parsed.action == "claim":
                conversation, _account, work, state = await claim_human_work_item_in_session(
                    session,
                    work_item_id=intent.human_work_item_id,
                    allowed_tenants=frozenset({tenant_id}),
                    actor=actor,
                    user_id=user_id,
                    expected_version=parsed.expected_work_version,
                    notification_public_id=parsed.notification_public_id,
                    expected_card_revision=parsed.expected_card_revision,
                    expected_action_nonce=parsed.action_nonce,
                )
                toast = "接单成功"
            else:
                conversation, _account, work, state = await resolve_human_work_item_in_session(
                    session,
                    work_item_id=intent.human_work_item_id,
                    allowed_tenants=frozenset({tenant_id}),
                    actor=actor,
                    expected_version=parsed.expected_work_version,
                    allow_override=False,
                    resolution_evidence="FEISHU_OPERATOR_ATTESTED",
                    notification_public_id=parsed.notification_public_id,
                    expected_card_revision=parsed.expected_card_revision,
                    expected_action_nonce=parsed.action_nonce,
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
        except (HumanWorkflowConflict, HandoffNotificationError):
            current_intent = await session.get(
                models.HandoffNotificationIntent,
                intent_db_id,
            )
            if current_intent is not None:
                await session.refresh(current_intent)
            card = (
                await _render_intent_card(session, current_intent)
                if current_intent is not None
                else None
            )
            response = _response("warning", "卡片状态已变化，已显示最新状态", card=card)
            receipt.outcome = "CONFLICT"
        except HumanWorkflowError:
            response = _response("error", "工单当前无法执行该操作")
            receipt.outcome = "CONFLICT"
        receipt.response_payload = response
        receipt.completed_at = datetime.now(UTC)
        await session.commit()
        return response
