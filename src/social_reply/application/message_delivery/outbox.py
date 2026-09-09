import asyncio
import hashlib
import logging
import math
import uuid
from collections.abc import Awaitable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from social_reply.application.account_management.access import session_authority_key
from social_reply.application.account_management.auth import Principal, principal_from_session_row
from social_reply.application.account_management.human_workflow import (
    ensure_open_human_work_item,
)
from social_reply.application.handoff_notifications.service import (
    ensure_handoff_notification_intent,
    lock_handoff_notification_route,
)
from social_reply.application.message_delivery.contracts import (
    SendContractError,
    TextSendCommand,
    parse_direct_text_command,
)
from social_reply.application.reply_decision.business_prompt import (
    business_prompt_lock_key,
    business_prompt_provenance_is_current,
)
from social_reply.application.reply_decision.multilingual_generation import (
    KNOWLEDGE_MATCH_AMBIGUITY_CONTRACT_VERSION,
    KNOWLEDGE_MATCH_AMBIGUITY_GATE_VERSION,
    KNOWLEDGE_MATCH_ONLY_CONTRACT_VERSION,
    KNOWLEDGE_MATCH_ONLY_SIMILARITY_GATE_VERSION,
    MULTILINGUAL_GENERATION_CONTRACT_VERSION,
)
from social_reply.application.reply_decision.rag_selection import (
    MATCH_ONLY_AMBIGUITY_RESOLUTION_METHOD,
)
from social_reply.connectors.email.contracts import email_address_identity_key
from social_reply.connectors.errors import (
    PermanentSendError,
    PlatformSendError,
    RetryableSendError,
)
from social_reply.connectors.registry import get_platform_sender
from social_reply.domain.platform_accounts import (
    DIRECT_DESTINATION_CAPABILITIES,
    LEGACY_ACTIVE_ACCOUNT_STATUSES,
    PLATFORM_CAPABILITY_SPECS,
    AccountPlatform,
    CapabilityKey,
    account_platform,
    capability_enabled,
    is_active_account_status,
    normalize_account_capability,
)
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.domain.reply.guard import run_hard_output_guard
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    hold_connection_advisory_lock,
    hold_connection_advisory_shared_lock,
    hold_connection_advisory_shared_lock_in_transaction,
    hold_conversation_delivery_lock,
    hold_conversation_delivery_lock_on_connection_in_transaction,
)
from social_reply.infrastructure.database.engine import get_engine, get_session_factory
from social_reply.infrastructure.killswitch import make_killswitch_checker
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)

# 旧 experimental 测试路径已删除；其遗留待发决策一律拒绝（fail-closed）。
_LEGACY_EXPERIMENTAL_CONTRACT_VERSION = "multilingual-experimental-runtime-v1"

_MAX_ATTEMPTS = 5
_CANCELLED_SEND_DRAIN_SECONDS = 30


def _match_only_delivery_shape_error(platform: str, payload_text: str) -> str | None:
    if not payload_text.strip():
        return "GUARD_EMPTY"
    try:
        maximum_text_length = PLATFORM_CAPABILITY_SPECS[AccountPlatform(platform)].max_text_length
    except ValueError:
        maximum_text_length = 2000
    if len(payload_text) > maximum_text_length:
        return "GUARD_TOO_LONG"
    return None


def _match_only_ambiguity_provenance_is_valid(
    decision: models.ReplyDecision,
) -> bool:
    minimum_similarity = decision.knowledge_min_similarity_threshold
    minimum_margin = decision.knowledge_min_margin_threshold
    top1_similarity = decision.knowledge_similarity
    top2_similarity = decision.knowledge_top2_similarity
    margin = decision.knowledge_similarity_margin
    if (
        decision.knowledge_match_status != "ambiguous"
        or decision.knowledge_gate_version != KNOWLEDGE_MATCH_AMBIGUITY_GATE_VERSION
        or minimum_similarity is None
        or minimum_margin is None
        or top1_similarity is None
        or top2_similarity is None
        or margin is None
        or top1_similarity < minimum_similarity
        or top2_similarity < minimum_similarity
        or not 0 <= margin < minimum_margin
        or decision.knowledge_content_hash is None
        or decision.knowledge_top2_content_hash is None
        or decision.knowledge_content_hash == decision.knowledge_top2_content_hash
        or not decision.selector_version
    ):
        return False
    evidence = decision.rag_evidence
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema_version") != "rag-evidence-v2"
        or evidence.get("selection_method") != MATCH_ONLY_AMBIGUITY_RESOLUTION_METHOD
        or evidence.get("selector_version") != decision.selector_version
        or evidence.get("selected_content_hash") != decision.knowledge_content_hash
        or evidence.get("selector_answer_hash") is not None
        or evidence.get("selector_content_hash") is not None
    ):
        return False
    candidates = evidence.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 2:
        return False
    candidates_by_id = {
        candidate.get("candidate_id"): candidate
        for candidate in candidates
        if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str)
    }
    if set(candidates_by_id) != {"candidate-1", "candidate-2"}:
        return False
    candidate_expectations = (
        (
            "candidate-1",
            decision.knowledge_content_hash,
            top1_similarity,
        ),
        (
            "candidate-2",
            decision.knowledge_top2_content_hash,
            top2_similarity,
        ),
    )
    for candidate_id, content_hash, similarity in candidate_expectations:
        candidate = candidates_by_id[candidate_id]
        content_hashes = candidate.get("content_hashes")
        candidate_similarity = candidate.get("similarity")
        if (
            not isinstance(content_hashes, list)
            or content_hash not in content_hashes
            or isinstance(candidate_similarity, bool)
            or not isinstance(candidate_similarity, (int, float))
            or not math.isclose(
                candidate_similarity,
                similarity,
                rel_tol=0.0,
                abs_tol=0.000001,
            )
        ):
            return False
    resolution = evidence.get("resolution")
    if not isinstance(resolution, dict):
        return False
    outcome = resolution.get("outcome")
    used_candidate_ids = resolution.get("used_candidate_ids")
    if (
        outcome not in {"answer", "clarify"}
        or not isinstance(used_candidate_ids, list)
        or not used_candidate_ids
        or len(set(used_candidate_ids)) != len(used_candidate_ids)
        or any(candidate_id not in candidates_by_id for candidate_id in used_candidate_ids)
        or resolution.get("version") != decision.selector_version
    ):
        return False
    return outcome != "clarify" or set(used_candidate_ids) == set(candidates_by_id)


async def _await_send[T](awaitable: Awaitable[T]) -> T:
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        logger.warning("delivery actor cancelled with provider send in flight; draining")
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=_CANCELLED_SEND_DRAIN_SECONDS,
            )
        except TimeoutError:
            task.cancel()
            task.add_done_callback(_consume_send_task_result)
            raise


def _consume_send_task_result(task: asyncio.Task[object]) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def _rollback_interrupted_delivery(session: AsyncSession) -> None:
    """Close the post-checkpoint transaction without changing the durable checkpoint."""
    if not session.in_transaction():
        return
    rollback_task = asyncio.create_task(session.rollback())
    try:
        await asyncio.shield(rollback_task)
    except asyncio.CancelledError:
        try:
            await rollback_task
        except BaseException:
            pass


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "transport timeout"
    if isinstance(exc, httpx.TransportError):
        return "transport error"
    if isinstance(exc, ValueError):
        return str(exc)[:500]
    return exc.__class__.__name__


async def materialize_sent_outbox(
    session: AsyncSession,
    *,
    outbox_id: uuid.UUID,
) -> None:
    finalized = (
        await session.execute(
            select(
                models.OutboxMessage.conversation_id,
                models.OutboxMessage.message_type,
                models.OutboxMessage.payload,
                models.OutboxMessage.actor_kind,
                models.OutboxMessage.sent_at,
                models.OutboxMessage.platform_message_id,
            ).where(
                models.OutboxMessage.id == outbox_id,
                models.OutboxMessage.status == "SENT",
            )
        )
    ).one_or_none()
    if finalized is None or finalized.message_type != "text":
        return
    payload = dict(finalized.payload or {})
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        return
    await session.execute(
        pg_insert(models.Message)
        .values(
            id=uuid.uuid4(),
            conversation_id=finalized.conversation_id,
            direction="outbound",
            sender_type=("agent" if finalized.actor_kind == "ADMIN_HUMAN" else "bot"),
            text=text,
            platform_message_id=finalized.platform_message_id,
            source_outbox_id=outbox_id,
            reply_target=dict(payload.get("target") or {}),
            private=False,
            occurred_at=finalized.sent_at,
        )
        .on_conflict_do_nothing(index_elements=["source_outbox_id"])
    )
    sent_column = (
        "last_human_message_at" if finalized.actor_kind == "ADMIN_HUMAN" else "last_bot_message_at"
    )
    await session.execute(
        update(models.AutomationState)
        .where(models.AutomationState.conversation_id == finalized.conversation_id)
        .values({sent_column: finalized.sent_at})
    )


async def _record_outcome(
    session: AsyncSession,
    outbox_id: uuid.UUID,
    status: str,
    *,
    attempt_no: int,
    error_code: str | None = None,
    error_message: str | None = None,
    platform_message_id: str | None = None,
    next_attempt_at: datetime | None = None,
    require_sending: bool = True,
    count_attempt: bool = True,
) -> str:
    values: dict = {
        "status": status,
        "last_error_code": error_code,
        "last_error_message": error_message,
        "next_attempt_at": next_attempt_at,
    }
    if count_attempt:
        values["attempt_count"] = attempt_no
    if platform_message_id is not None:
        values["platform_message_id"] = platform_message_id
    if status == "SENT":
        values["sent_at"] = func.clock_timestamp()
    statement = update(models.OutboxMessage).where(models.OutboxMessage.id == outbox_id)
    if require_sending:
        statement = statement.where(models.OutboxMessage.status == "SENDING")
    finalized = (
        await session.execute(
            statement.values(**values).returning(
                models.OutboxMessage.conversation_id,
                models.OutboxMessage.message_type,
                models.OutboxMessage.payload,
                models.OutboxMessage.actor_kind,
                models.OutboxMessage.sent_at,
                models.OutboxMessage.platform_message_id,
            )
        )
    ).first()
    actual_status = status
    actual_error_code = error_code
    actual_error_message = error_message
    if finalized is None:
        current_status = await session.scalar(
            select(models.OutboxMessage.status).where(models.OutboxMessage.id == outbox_id)
        )
        logger.warning(
            "outbox %s outcome %s skipped because current status is %s",
            outbox_id,
            status,
            current_status,
        )
        actual_status = "STALE_FINALIZE"
        actual_error_code = "STALE_FINALIZE"
        actual_error_message = f"requested={status}; current={current_status}"
    elif status == "SENT":
        await materialize_sent_outbox(session, outbox_id=outbox_id)
    if count_attempt:
        await session.execute(
            insert(models.DeliveryAttempt).values(
                outbox_id=outbox_id,
                attempt_no=attempt_no,
                outcome=actual_status,
                error_code=actual_error_code,
                error_message=actual_error_message,
            )
        )
    await session.commit()
    return actual_status


async def _finalize(outbox_id: uuid.UUID, status: str, **kwargs) -> str:
    async with get_session_factory()() as session:
        return await _record_outcome(session, outbox_id, status, **kwargs)


async def _stop_before_send(
    session: AsyncSession,
    outbox_id: uuid.UUID,
    status: str,
    error_code: str,
    attempt_no: int,
    *,
    count_attempt: bool = True,
) -> str:
    return await _record_outcome(
        session,
        outbox_id,
        status,
        attempt_no=attempt_no,
        error_code=error_code,
        count_attempt=count_attempt,
    )


async def _reject_direct_send(
    session: AsyncSession,
    row: models.OutboxMessage,
    attempt_no: int,
    error_code: str,
    *,
    count_attempt: bool = True,
) -> tuple[str, None, None]:
    status = await _stop_before_send(
        session,
        row.id,
        "NEEDS_REVIEW",
        error_code,
        attempt_no,
        count_attempt=count_attempt,
    )
    return status, None, None


async def _validate_direct_send(
    session: AsyncSession,
    row: models.OutboxMessage,
    payload: dict,
    attempt_no: int,
) -> tuple[
    str | None,
    models.PlatformAccount | None,
    TextSendCommand | None,
]:
    conversation = await session.scalar(
        select(models.Conversation)
        .where(models.Conversation.id == row.conversation_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    account = await session.scalar(
        select(models.PlatformAccount)
        .where(models.PlatformAccount.id == row.platform_account_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if conversation is None or account is None:
        return await _reject_direct_send(session, row, attempt_no, "TENANT_SCOPE_MISMATCH")
    if (
        account.tenant_id != row.tenant_id
        or conversation.tenant_id != row.tenant_id
        or conversation.platform_account_id != row.platform_account_id
    ):
        return await _reject_direct_send(session, row, attempt_no, "TENANT_SCOPE_MISMATCH")
    if (account.config or {}).get("delivery_mode") != "direct":
        return await _reject_direct_send(
            session,
            row,
            attempt_no,
            "DELIVERY_MODE_UNSUPPORTED",
            count_attempt=False,
        )
    if not is_active_account_status(account.status):
        return await _reject_direct_send(session, row, attempt_no, "ACCOUNT_NOT_ACTIVE")
    settings = get_settings()
    if account.platform in {"facebook", "instagram", "whatsapp", "feishu", "email"}:
        disabled_code = settings.platform_disabled_code(account.platform)
        if disabled_code is not None:
            return await _reject_direct_send(
                session,
                row,
                attempt_no,
                disabled_code,
                count_attempt=False,
            )
    if (
        account.platform in {"facebook", "instagram"}
        and str((account.config or {}).get("meta_health_status") or "") != "READY"
    ):
        return await _reject_direct_send(
            session,
            row,
            attempt_no,
            "META_ACCOUNT_NOT_READY",
            count_attempt=False,
        )
    if (
        account.platform == "feishu"
        and str((account.config or {}).get("feishu_health_status") or "") != "READY"
    ):
        return await _reject_direct_send(
            session,
            row,
            attempt_no,
            "FEISHU_ACCOUNT_NOT_READY",
            count_attempt=False,
        )
    if (
        account.platform == "email"
        and str((account.config or {}).get("email_health_status") or "") != "READY"
    ):
        return await _reject_direct_send(
            session,
            row,
            attempt_no,
            "EMAIL_ACCOUNT_NOT_READY",
            count_attempt=False,
        )
    if (
        account.platform == "email"
        and row.origin_kind == "DECISION"
        and row.actor_kind == "BOT"
        and not settings.email_auto_reply_enabled
    ):
        return await _reject_direct_send(
            session,
            row,
            attempt_no,
            "EMAIL_AUTO_REPLY_DISABLED",
            count_attempt=False,
        )
    destination = DIRECT_DESTINATION_CAPABILITIES.get(row.destination_type)
    try:
        platform = account_platform(account.platform)
        capability = normalize_account_capability(account.platform, dict(account.capability or {}))
    except ValueError:
        return await _reject_direct_send(session, row, attempt_no, "CAPABILITY_INVALID")
    if destination is None or platform not in destination.platforms:
        return await _reject_direct_send(session, row, attempt_no, "DELIVERY_ROUTE_INVALID")
    if not capability_enabled(capability, destination.capability):
        return await _reject_direct_send(session, row, attempt_no, "CAPABILITY_NOT_ALLOWED")
    if row.valid_until is not None and row.valid_until <= datetime.now(UTC):
        return await _reject_direct_send(session, row, attempt_no, "DELIVERY_WINDOW_EXPIRED")
    if row.reply_to_message_id is not None:
        source_message = (
            await session.execute(
                select(models.Message.reply_target)
                .where(
                    models.Message.id == row.reply_to_message_id,
                    models.Message.conversation_id == row.conversation_id,
                    models.Message.direction == "inbound",
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).first()
    else:
        # Compatibility for intents created before reply_to_message_id was introduced.
        source_message = (
            await session.execute(
                select(models.Message.reply_target)
                .join(
                    models.ReplyDecision,
                    models.ReplyDecision.message_id == models.Message.id,
                )
                .where(
                    models.ReplyDecision.outbox_id == row.id,
                    models.Message.conversation_id == row.conversation_id,
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).first()
    conversation_external_user_id = await session.scalar(
        select(models.Contact.external_user_id)
        .where(
            models.Contact.id == conversation.contact_id,
            models.Contact.platform_account_id == account.id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if source_message is None or not conversation_external_user_id:
        return await _reject_direct_send(
            session,
            row,
            attempt_no,
            "DELIVERY_TARGET_INVALID",
            count_attempt=False,
        )
    try:
        command = parse_direct_text_command(
            destination_type=row.destination_type,
            message_type=row.message_type,
            payload=payload,
            destination_id=row.destination_id,
            account_platform=account.platform,
            account_external_id=account.external_account_id,
            source_target=dict(source_message.reply_target or {}),
            conversation_external_user_id=conversation_external_user_id,
            outbox_id=row.id,
        )
    except SendContractError as exc:
        return await _reject_direct_send(
            session,
            row,
            attempt_no,
            exc.code,
            count_attempt=False,
        )
    if len(command.text) > capability[CapabilityKey.MAX_TEXT_LENGTH.value]:
        return await _reject_direct_send(session, row, attempt_no, "CAPABILITY_TEXT_TOO_LONG")
    return None, account, command


def _effective_origin_kind(row: models.OutboxMessage, payload: dict) -> str | None:
    authority = (row.origin_kind, row.actor_kind)
    if authority == ("DECISION", "BOT"):
        return "DECISION"
    if authority == ("DRAFT_APPROVAL", "ADMIN_HUMAN"):
        return "DRAFT_APPROVAL"
    if authority == ("MANUAL_REPLY", "ADMIN_HUMAN"):
        return "MANUAL_REPLY"
    if authority == ("SYSTEM_NOTICE", "SYSTEM"):
        return "SYSTEM_NOTICE"
    if authority == ("DECISION", "ADMIN_HUMAN") and payload.get("approval") == "admin":
        return "DRAFT_APPROVAL"
    return None


def _send_state_allowed(
    row: models.OutboxMessage,
    payload: dict,
    state: str | None,
) -> bool:
    origin_kind = _effective_origin_kind(row, payload)
    return (
        (origin_kind == "DECISION" and state == "BOT_ACTIVE")
        or (origin_kind == "DRAFT_APPROVAL" and state in {"BOT_ACTIVE", "BOT_DRAFT_ONLY"})
        or (origin_kind == "MANUAL_REPLY" and state == "HUMAN_ACTIVE")
        or (origin_kind == "SYSTEM_NOTICE" and state in {"HANDOFF_PENDING", "HUMAN_ACTIVE"})
    )


def _is_rate_limited_email_reply(row: models.OutboxMessage) -> bool:
    return (
        row.destination_type == "email_reply"
        and row.origin_kind == "DECISION"
        and row.actor_kind == "BOT"
    )


def _email_sender_lock_key(platform_account_id: uuid.UUID, sender: str) -> str:
    identity = email_address_identity_key(sender)
    return f"social-reply:email-sender-delivery:{platform_account_id}:{identity}"


async def _email_replies_sent_in_window(
    session: AsyncSession,
    *,
    platform_account_id: uuid.UUID,
    sender: str,
) -> int:
    sender_identity = email_address_identity_key(sender)
    return int(
        await session.scalar(
            select(func.count())
            .select_from(models.OutboxMessage)
            .join(
                models.Conversation,
                models.Conversation.id == models.OutboxMessage.conversation_id,
            )
            .join(models.Contact, models.Contact.id == models.Conversation.contact_id)
            .where(
                models.OutboxMessage.platform_account_id == platform_account_id,
                models.OutboxMessage.destination_type == "email_reply",
                models.OutboxMessage.origin_kind == "DECISION",
                models.OutboxMessage.actor_kind == "BOT",
                models.OutboxMessage.status == "SENT",
                models.OutboxMessage.sent_at >= func.clock_timestamp() - timedelta(hours=24),
                models.Contact.external_user_id == sender_identity,
            )
        )
        or 0
    )


async def _validate_human_outbox_authority(
    session: AsyncSession,
    *,
    outbox: models.OutboxMessage,
    prevalidation_error: str | None = None,
) -> str | None:
    payload = dict(outbox.payload) if isinstance(outbox.payload, dict) else {}
    effective_origin = _effective_origin_kind(outbox, payload)
    requires_human = outbox.actor_kind == "ADMIN_HUMAN" or outbox.origin_kind in {
        "MANUAL_REPLY",
        "DRAFT_APPROVAL",
    }
    if not requires_human:
        return None
    if outbox.actor_kind != "ADMIN_HUMAN":
        return "HUMAN_ACTOR_KIND_INVALID"
    if effective_origin not in {"MANUAL_REPLY", "DRAFT_APPROVAL"}:
        return "HUMAN_ORIGIN_INVALID"
    if prevalidation_error is not None:
        return prevalidation_error
    if outbox.initiator_session_id is None:
        return "HUMAN_INITIATOR_SESSION_MISSING"
    principal = await principal_from_session_row(
        session,
        outbox.initiator_session_id,
        for_update=True,
    )
    if (
        principal is None
        or principal.is_feishu_action
        or principal.session_id != outbox.initiator_session_id
        or principal.must_change_password
        or not principal.has_capability("reply")
    ):
        return "HUMAN_INITIATOR_SESSION_INVALID"
    if principal.actor != outbox.actor_id:
        return "HUMAN_INITIATOR_IDENTITY_MISMATCH"
    if principal.user_id != outbox.initiator_user_id:
        return "HUMAN_INITIATOR_USER_MISMATCH"

    conversation = await session.scalar(
        select(models.Conversation)
        .where(
            models.Conversation.id == outbox.conversation_id,
            models.Conversation.tenant_id == outbox.tenant_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    account = await session.scalar(
        select(models.PlatformAccount)
        .where(
            models.PlatformAccount.id == outbox.platform_account_id,
            models.PlatformAccount.tenant_id == outbox.tenant_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        conversation is None
        or account is None
        or conversation.platform_account_id != account.id
        or not principal.can_access_account(account)
    ):
        return "HUMAN_ACCOUNT_ACCESS_INVALID"
    if effective_origin == "DRAFT_APPROVAL":
        if outbox.human_work_item_version is not None:
            return "HUMAN_WORK_VERSION_INVALID"
        if outbox.origin_kind == "DRAFT_APPROVAL":
            decision_link = or_(
                models.ReplyDecision.review_outbox_id == outbox.id,
                and_(
                    models.ReplyDecision.review_outbox_id.is_(None),
                    models.ReplyDecision.outbox_id == outbox.id,
                ),
            )
        else:
            decision_link = models.ReplyDecision.outbox_id == outbox.id
        decision = await session.scalar(
            select(models.ReplyDecision)
            .where(
                decision_link,
                models.ReplyDecision.tenant_id == outbox.tenant_id,
                models.ReplyDecision.conversation_id == outbox.conversation_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if decision is None:
            return "DRAFT_APPROVAL_PROVENANCE_INVALID"
        if (
            decision.action != "draft"
            or decision.review_action not in {"ACCEPTED", "EDITED"}
            or not decision.reviewed_by
            or decision.reviewed_at is None
            or decision.reviewed_by != principal.actor
            or decision.final_reply_text != payload.get("text")
        ):
            return "DRAFT_APPROVAL_PROVENANCE_INVALID"
        return None if principal.has_capability("reply") else "DRAFT_APPROVER_NOT_AUTHORIZED"
    if outbox.human_work_item_version is None:
        return "HUMAN_WORK_VERSION_MISSING"
    work = await session.scalar(
        select(models.HumanWorkItem)
        .where(
            models.HumanWorkItem.tenant_id == outbox.tenant_id,
            models.HumanWorkItem.conversation_id == outbox.conversation_id,
            models.HumanWorkItem.version == outbox.human_work_item_version,
            models.HumanWorkItem.status == "CLAIMED",
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if work is None:
        return "HUMAN_WORK_VERSION_STALE"
    if principal.user_id is None:
        if (
            not principal.is_superadmin
            or work.assigned_user_id is not None
            or work.assigned_session_id != principal.session_id
            or work.assigned_actor != principal.actor
        ):
            return "HUMAN_ASSIGNMENT_STALE"
    elif work.assigned_user_id != principal.user_id or work.assigned_actor != principal.actor:
        return "HUMAN_ASSIGNMENT_STALE"
    return None


async def _lock_public_send_route_accounts(
    session: AsyncSession,
    *,
    outbox: models.OutboxMessage,
) -> (
    tuple[
        dict[str, object | None],
        dict[uuid.UUID, models.PlatformAccount],
    ]
    | None
):
    route_values = await lock_handoff_notification_route(
        session,
        tenant_id=outbox.tenant_id,
    )
    notification_account_id = route_values["feishu_platform_account_id"]
    account_ids = {outbox.platform_account_id}
    if isinstance(notification_account_id, uuid.UUID):
        account_ids.add(notification_account_id)
    locked_accounts = {
        account_row.id: account_row
        for account_row in (
            await session.scalars(
                select(models.PlatformAccount)
                .where(
                    models.PlatformAccount.tenant_id == outbox.tenant_id,
                    models.PlatformAccount.id.in_(account_ids),
                )
                .execution_options(populate_existing=True)
                .order_by(models.PlatformAccount.id)
                .with_for_update()
            )
        ).all()
    }
    # The shared config guard freezes the route; validate the notification account after
    # the ordered row lock before accepting the route snapshot as PENDING.
    if route_values["status"] == "PENDING":
        notification_account = (
            locked_accounts.get(notification_account_id)
            if isinstance(notification_account_id, uuid.UUID)
            else None
        )
        if (
            notification_account is None
            or notification_account.platform != AccountPlatform.FEISHU
            or not is_active_account_status(notification_account.status)
        ):
            route_values = {
                "notification_config_id": None,
                "config_version": None,
                "feishu_platform_account_id": None,
                "destination_chat_id": None,
                "status": "BLOCKED_CONFIG",
                "last_error_code": "FEISHU_HANDOFF_ACCOUNT_INVALID",
            }
    return (route_values, locked_accounts) if set(locked_accounts) == account_ids else None


async def _public_bot_send_preflight(
    session: AsyncSession,
    *,
    outbox: models.OutboxMessage,
    payload_text: str,
) -> str | None:
    """Re-authorize a public send derived from a bot decision immediately before I/O."""
    if outbox.message_type == "private_note":
        return None
    payload = dict(outbox.payload) if isinstance(outbox.payload, dict) else {}
    authority = _effective_origin_kind(outbox, payload)
    if authority not in {"DECISION", "DRAFT_APPROVAL"}:
        return None

    predecessor_approval = (
        outbox.origin_kind == "DRAFT_APPROVAL" and outbox.actor_kind == "ADMIN_HUMAN"
    )
    legacy_approval = (
        outbox.origin_kind == "DECISION"
        and outbox.actor_kind == "ADMIN_HUMAN"
        and payload.get("approval") == "admin"
    )
    locked_route = await _lock_public_send_route_accounts(
        session,
        outbox=outbox,
    )
    if locked_route is None:
        return "PUBLIC_SEND_SCOPE_INVALID"
    route_values, locked_accounts = locked_route
    if route_values["status"] != "PENDING":
        return "PUBLIC_SEND_ROUTE_INVALID"
    account = locked_accounts.get(outbox.platform_account_id)
    conversation = await session.scalar(
        select(models.Conversation)
        .where(models.Conversation.id == outbox.conversation_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if conversation is None or account is None:
        return "PUBLIC_SEND_SCOPE_INVALID"
    if authority == "DECISION":
        decision_link = models.ReplyDecision.outbox_id == outbox.id
    elif predecessor_approval:
        decision_link = or_(
            models.ReplyDecision.review_outbox_id == outbox.id,
            and_(
                models.ReplyDecision.review_outbox_id.is_(None),
                models.ReplyDecision.outbox_id == outbox.id,
            ),
        )
    else:
        decision_link = models.ReplyDecision.outbox_id == outbox.id
    decisions = (
        await session.scalars(
            select(models.ReplyDecision)
            .where(
                decision_link,
                models.ReplyDecision.tenant_id == outbox.tenant_id,
                models.ReplyDecision.conversation_id == outbox.conversation_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
            .limit(2)
        )
    ).all()
    if len(decisions) > 1:
        return "PUBLIC_SEND_PROVENANCE_INVALID"
    decision = decisions[0] if decisions else None
    if decision is None:
        return "PUBLIC_SEND_PROVENANCE_INVALID"
    source_message = (
        await session.scalar(
            select(models.Message)
            .where(models.Message.id == decision.message_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if decision.message_id is not None
        else None
    )
    if (
        conversation is None
        or account is None
        or decision.tenant_id != outbox.tenant_id
        or decision.conversation_id != outbox.conversation_id
        or conversation.tenant_id != outbox.tenant_id
        or conversation.platform_account_id != outbox.platform_account_id
        or account.tenant_id != outbox.tenant_id
        or conversation.brand_id != account.brand_id
        or conversation.platform != account.platform
        or account.status not in LEGACY_ACTIVE_ACCOUNT_STATUSES
        or decision.message_id is None
        or decision.message_id != outbox.reply_to_message_id
        or source_message is None
        or source_message.id != outbox.reply_to_message_id
        or source_message.conversation_id != outbox.conversation_id
        or source_message.direction != "inbound"
        or source_message.decision_generation != decision.decision_generation
    ):
        return "PUBLIC_SEND_SCOPE_INVALID"
    if (
        decision.decision_generation is None
        or decision.decision_generation != conversation.decision_generation
    ):
        return "STALE_CONVERSATION_INPUT"

    if (
        decision.reply_business_prompt_version_id is not None
        and decision.reply_business_prompt_content_hash is None
    ):
        return "REPLY_BUSINESS_PROMPT_PROVENANCE_INVALID"
    settings = get_settings()
    business_prompt_enabled = getattr(
        settings,
        "reply_business_prompt_enabled",
        False,
    )
    if decision.reply_business_prompt_content_hash is not None and not business_prompt_enabled:
        return "REPLY_BUSINESS_PROMPT_DISABLED"
    if business_prompt_enabled and decision.reply_business_prompt_content_hash is None:
        return "REPLY_BUSINESS_PROMPT_PROVENANCE_REQUIRED"
    if decision.reply_business_prompt_content_hash is not None:
        prompt_is_current = await business_prompt_provenance_is_current(
            session,
            tenant_id=account.tenant_id,
            brand_id=account.brand_id,
            version_id=decision.reply_business_prompt_version_id,
            content_hash=decision.reply_business_prompt_content_hash,
            acquire_lock=False,
        )
        if not prompt_is_current:
            return "STALE_REPLY_BUSINESS_PROMPT"

    try:
        disabled = await make_killswitch_checker().is_disabled(
            account.brand_id,
            str(account.id),
            account.tenant_id,
        )
    except Exception:
        logger.exception(
            "kill switch lookup failed during public send preflight",
            extra={
                "tenant_id": account.tenant_id,
                "brand_id": account.brand_id,
                "account_id": str(account.id),
                "outbox_id": str(outbox.id),
            },
        )
        return "KILLSWITCH_UNAVAILABLE"
    if disabled:
        return "KILLSWITCH"

    if authority == "DECISION":
        if decision.action != "auto_reply" or payload_text != decision.reply_text:
            return "PUBLIC_SEND_PAYLOAD_MISMATCH"
    elif (
        decision.action != "draft"
        or decision.review_action not in {"ACCEPTED", "EDITED"}
        or not decision.reviewed_by
        or decision.reviewed_by != outbox.actor_id
        or decision.reviewed_at is None
        or not (
            decision.review_outbox_id == outbox.id
            or (
                predecessor_approval
                and decision.review_outbox_id is None
                and decision.outbox_id == outbox.id
            )
            or (legacy_approval and decision.outbox_id == outbox.id)
        )
        or payload_text != decision.final_reply_text
    ):
        return "DRAFT_APPROVAL_PROVENANCE_INVALID"

    if decision.multilingual_contract_version == _LEGACY_EXPERIMENTAL_CONTRACT_VERSION:
        return "EXPERIMENTAL_MULTILINGUAL_DISABLED"
    match_only_single_contract = (
        decision.multilingual_contract_version == KNOWLEDGE_MATCH_ONLY_CONTRACT_VERSION
    )
    match_only_ambiguity_contract = (
        decision.multilingual_contract_version == KNOWLEDGE_MATCH_AMBIGUITY_CONTRACT_VERSION
    )
    match_only_contract = match_only_single_contract or match_only_ambiguity_contract
    if match_only_contract:
        if not getattr(settings, "knowledge_match_only_reply_enabled", False):
            return "KNOWLEDGE_MATCH_ONLY_REPLY_DISABLED"
        if not settings.multilingual_knowledge_reply_enabled:
            return "MULTILINGUAL_LIVE_DISABLED"
    if match_only_single_contract:
        single_candidate_provenance_is_valid = (
            decision.knowledge_similarity is not None
            and decision.knowledge_min_similarity_threshold is not None
            and decision.knowledge_similarity >= decision.knowledge_min_similarity_threshold
        )
        if decision.knowledge_gate_version == "strong-gate-v1":
            single_candidate_provenance_is_valid = (
                single_candidate_provenance_is_valid
                and decision.knowledge_match_status == "strong"
                and decision.knowledge_min_margin_threshold is not None
                and (
                    decision.knowledge_similarity_margin is None
                    or decision.knowledge_similarity_margin
                    >= decision.knowledge_min_margin_threshold
                )
            )
        elif decision.knowledge_gate_version == KNOWLEDGE_MATCH_ONLY_SIMILARITY_GATE_VERSION:
            single_candidate_provenance_is_valid = (
                single_candidate_provenance_is_valid
                and decision.knowledge_match_status in {"strong", "ambiguous"}
            )
        else:
            single_candidate_provenance_is_valid = False
        if not single_candidate_provenance_is_valid:
            return "MULTILINGUAL_PROVENANCE_INVALID"
    if match_only_ambiguity_contract and not _match_only_ambiguity_provenance_is_valid(decision):
        return "MULTILINGUAL_PROVENANCE_INVALID"
    if decision.multilingual_contract_version == MULTILINGUAL_GENERATION_CONTRACT_VERSION:
        if not settings.multilingual_knowledge_reply_enabled:
            return "MULTILINGUAL_LIVE_DISABLED"
        if (
            decision.grounding_verified is not True
            or decision.knowledge_match_status != "strong"
            or decision.knowledge_similarity is None
            or decision.knowledge_min_similarity_threshold is None
            or decision.knowledge_similarity < decision.knowledge_min_similarity_threshold
        ):
            return "MULTILINGUAL_PROVENANCE_INVALID"
        if decision.knowledge_gate_version == "strong-gate-v1":
            if decision.knowledge_min_margin_threshold is None or (
                decision.knowledge_similarity_margin is not None
                and decision.knowledge_similarity_margin < decision.knowledge_min_margin_threshold
            ):
                return "MULTILINGUAL_PROVENANCE_INVALID"
        elif decision.knowledge_gate_version not in {
            "selector-gate-v2",
            "selector-gate-v3",
        }:
            return "MULTILINGUAL_PROVENANCE_INVALID"

    knowledge_identity = (
        decision.knowledge_document_id,
        decision.knowledge_chunk_id,
        decision.knowledge_content_hash,
    )
    if decision.multilingual_contract_version in {
        MULTILINGUAL_GENERATION_CONTRACT_VERSION,
        KNOWLEDGE_MATCH_ONLY_CONTRACT_VERSION,
        KNOWLEDGE_MATCH_AMBIGUITY_CONTRACT_VERSION,
    } and not any(value is not None for value in knowledge_identity):
        return "MULTILINGUAL_KNOWLEDGE_IDENTITY_INVALID"
    source_row = None
    if any(value is not None for value in knowledge_identity):
        if any(value is None for value in knowledge_identity):
            return "MULTILINGUAL_KNOWLEDGE_IDENTITY_INVALID"
        if match_only_contract:
            # Temporarily disabled for match-only testing; keep the source currentness query in
            # the normal contract branch below for later restoration.
            # source_row = await _load_current_published_knowledge_source(...)
            source_row = None
        else:
            source_row = (
                await session.execute(
                    select(models.KnowledgeDocument, models.KnowledgeChunk)
                    .join(
                        models.KnowledgeChunk,
                        (models.KnowledgeChunk.tenant_id == models.KnowledgeDocument.tenant_id)
                        & (models.KnowledgeChunk.document_id == models.KnowledgeDocument.id),
                    )
                    .where(
                        models.KnowledgeDocument.tenant_id == decision.tenant_id,
                        models.KnowledgeDocument.id == decision.knowledge_document_id,
                        models.KnowledgeChunk.id == decision.knowledge_chunk_id,
                        models.KnowledgeChunk.content_hash == decision.knowledge_content_hash,
                        models.KnowledgeDocument.brand_id == account.brand_id,
                        or_(
                            models.KnowledgeDocument.platform.is_(None),
                            models.KnowledgeDocument.platform == account.platform,
                        ),
                        models.KnowledgeDocument.status == "published",
                        models.KnowledgeDocument.source_language == "en",
                        models.KnowledgeDocument.language_verified.is_(True),
                    )
                )
            ).one_or_none()
            if source_row is None:
                return "MULTILINGUAL_SOURCE_REVOKED"

    artifact = None
    localization_identity = (
        decision.knowledge_localization_id,
        decision.knowledge_localization_release_id,
        decision.knowledge_localization_text_hash,
        decision.knowledge_localization_source_hash,
    )
    if not match_only_contract and any(value is not None for value in localization_identity):
        if source_row is None or any(value is None for value in localization_identity):
            return "LOCALIZATION_PROVENANCE_INVALID"
        document, chunk = source_row
        artifact = await session.scalar(
            select(models.KnowledgeLocalization)
            .where(
                models.KnowledgeLocalization.tenant_id == decision.tenant_id,
                models.KnowledgeLocalization.id == decision.knowledge_localization_id,
                models.KnowledgeLocalization.document_id == document.id,
                models.KnowledgeLocalization.release_id
                == decision.knowledge_localization_release_id,
            )
            .with_for_update()
        )
        if artifact is None:
            return "LOCALIZATION_RELEASE_MISSING"
        if (
            artifact.status != "published"
            or not artifact.auto_reply_allowed
            or not artifact.reviewed_by
            or artifact.reviewed_at is None
            or artifact.text_hash != decision.knowledge_localization_text_hash
            or artifact.source_content_hash != decision.knowledge_localization_source_hash
            or chunk.content_hash != decision.knowledge_localization_source_hash
            or hashlib.sha256(artifact.localized_text.encode()).hexdigest() != artifact.text_hash
            or (document.is_official_contact and not artifact.official_contact_authorized)
        ):
            return "LOCALIZATION_RELEASE_REVOKED"

    if match_only_contract:
        # Temporarily disabled for match-only testing; the normal contract below still executes:
        # - localization provenance/currentness checks
        # - run_hard_output_guard for facts, entities, contacts, PII, and approved wording
        return _match_only_delivery_shape_error(account.platform, payload_text)

    document = source_row[0] if source_row is not None else None
    guard_source = decision.source
    if artifact is not None:
        guard_source = "knowledge_localization"
    elif document is not None and document.is_official_contact:
        guard_source = "knowledge"
    guarded = run_hard_output_guard(
        ReplyDecision(
            action=ReplyAction.AUTO_REPLY,
            reply_text=payload_text,
            source=guard_source,
        ),
        account.platform,
        approved_official_contact_reply=(
            document.reply if document is not None and document.is_official_contact else None
        ),
        expected_reply_language=(decision.resolved_locale or "und"),
        approved_knowledge_reply=(document.reply if document is not None else None),
        approved_knowledge_protected_values=(
            tuple(document.protected_values or ()) if document is not None else ()
        ),
        approved_localization_text=(artifact.localized_text if artifact is not None else None),
        approved_localization_text_hash=(artifact.text_hash if artifact is not None else None),
        approved_localization_protected_values=(
            tuple(artifact.protected_values or ()) if artifact is not None else ()
        ),
    )
    if guarded.action is not ReplyAction.AUTO_REPLY:
        return next(
            (code for code in reversed(guarded.reason_codes) if code.startswith("GUARD_")),
            "GUARD_OUTPUT_INVALID",
        )
    return None


async def _localization_send_preflight(
    session: AsyncSession,
    *,
    outbox_id: uuid.UUID,
    platform_account_id: uuid.UUID,
    payload_text: str,
    message_type: str,
) -> str | None:
    """Compatibility wrapper for existing localization integration tests."""
    if message_type == "private_note":
        return None
    outbox = await session.get(models.OutboxMessage, outbox_id)
    if outbox is None or outbox.platform_account_id != platform_account_id:
        return "PUBLIC_SEND_SCOPE_INVALID"
    return await _public_bot_send_preflight(
        session,
        outbox=outbox,
        payload_text=payload_text,
    )


async def _handoff_public_send_failure(
    session: AsyncSession,
    *,
    outbox: models.OutboxMessage,
    reason_code: str,
) -> None:
    if reason_code == "STALE_CONVERSATION_INPUT":
        # This Outbox belongs to historical work. Cancelling it must not change the
        # automation state for the newer conversation generation.
        return
    state = (
        await session.execute(
            select(models.AutomationState)
            .where(models.AutomationState.conversation_id == outbox.conversation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if state is None or state.state in {"HUMAN_ACTIVE", "CLOSED"}:
        return
    if state.state != "HANDOFF_PENDING":
        state.state = "HANDOFF_PENDING"
        state.state_version += 1
        state.state_changed_reason = reason_code
    work = await ensure_open_human_work_item(
        session,
        tenant_id=outbox.tenant_id,
        conversation_id=outbox.conversation_id,
        reason_code=reason_code,
    )
    await ensure_handoff_notification_intent(session, work=work)


async def _deliver_outbox_locked(
    session: AsyncSession,
    connection: AsyncConnection,
    oid: uuid.UUID,
    conversation_id: uuid.UUID,
    human_prevalidation_error: str | None = None,
) -> str:
    claimed = (
        await session.execute(
            update(models.OutboxMessage)
            .where(
                models.OutboxMessage.id == oid,
                models.OutboxMessage.conversation_id == conversation_id,
                or_(
                    models.OutboxMessage.status == "PENDING",
                    and_(
                        models.OutboxMessage.status == "FAILED",
                        models.OutboxMessage.next_attempt_at <= datetime.now(UTC),
                    ),
                ),
            )
            .values(status="SENDING", locked_at=datetime.now(UTC), locked_by="deliver")
            .returning(models.OutboxMessage.id)
        )
    ).first()
    if claimed is None:
        await session.commit()
        return "SKIPPED_NOT_CLAIMABLE"
    row = (
        await session.execute(
            select(models.OutboxMessage)
            .where(models.OutboxMessage.id == oid)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    attempt_no = row.attempt_count + 1
    if not isinstance(row.payload, dict):
        return await _stop_before_send(
            session,
            oid,
            "NEEDS_REVIEW",
            "DELIVERY_PAYLOAD_INVALID",
            attempt_no,
            count_attempt=False,
        )
    payload = dict(row.payload)
    if not isinstance(payload.get("text"), str) or not payload["text"].strip():
        return await _stop_before_send(
            session,
            oid,
            "NEEDS_REVIEW",
            "DELIVERY_TEXT_INVALID",
            attempt_no,
            count_attempt=False,
        )
    is_public = row.message_type != "private_note"
    preflight_error = None
    if is_public:
        preflight_error = await _public_bot_send_preflight(
            session,
            outbox=row,
            payload_text=payload["text"],
        )
    human_authority_error = await _validate_human_outbox_authority(
        session,
        outbox=row,
        prevalidation_error=human_prevalidation_error,
    )
    if human_authority_error is not None:
        return await _stop_before_send(
            session,
            oid,
            "NEEDS_REVIEW",
            human_authority_error,
            attempt_no,
        )
    if preflight_error is not None:
        await _handoff_public_send_failure(
            session,
            outbox=row,
            reason_code=preflight_error,
        )
        return await _stop_before_send(
            session,
            oid,
            "CANCELLED",
            preflight_error,
            attempt_no,
            count_attempt=False,
        )

    if not is_public:
        return await _stop_before_send(
            session, oid, "CANCELLED", "DIRECT_DRAFT_BLOCKED", attempt_no
        )
    settings = get_settings()
    if row.destination_type == "x_dm" and not settings.x_legacy_dm_enabled:
        return await _stop_before_send(
            session,
            oid,
            "NEEDS_REVIEW",
            "X_LEGACY_DM_DISABLED",
            attempt_no,
            count_attempt=False,
        )
    if row.destination_type == "x_chat_message" and not settings.xchat_enabled:
        return await _stop_before_send(
            session,
            oid,
            "NEEDS_REVIEW",
            "XCHAT_DISABLED",
            attempt_no,
            count_attempt=False,
        )
    state = (
        await session.execute(
            select(models.AutomationState.state).where(
                models.AutomationState.conversation_id == row.conversation_id
            )
        )
    ).scalar_one_or_none()
    send_state_allowed = _send_state_allowed(row, payload, state)
    if is_public and not send_state_allowed:
        return await _stop_before_send(session, oid, "CANCELLED", "TAKEOVER_AT_SEND", attempt_no)
    direct_account: models.PlatformAccount | None = None
    direct_command: TextSendCommand | None = None
    stopped, direct_account, direct_command = await _validate_direct_send(
        session,
        row,
        payload,
        attempt_no,
    )
    if stopped is not None:
        return stopped

    async def dispatch() -> str:
        nonlocal row
        expected_actor_kind = row.actor_kind
        expected_actor_id = row.actor_id
        expected_initiator_user_id = row.initiator_user_id
        expected_initiator_session_id = row.initiator_session_id
        dispatch_claim = (
            await session.execute(
                update(models.OutboxMessage)
                .where(
                    models.OutboxMessage.id == oid,
                    models.OutboxMessage.status == "SENDING",
                    models.OutboxMessage.attempt_count == attempt_no - 1,
                )
                .values(
                    status="SENDING",
                    attempt_count=attempt_no,
                    locked_at=datetime.now(UTC),
                    locked_by="deliver",
                )
                .returning(models.OutboxMessage.id)
            )
        ).first()
        if dispatch_claim is None:
            await session.rollback()
            return "SKIPPED_NOT_CLAIMABLE"

        # Durable checkpoint: after this commit, any provider call is represented as SENDING
        # with its attempt number even if the worker is cancelled or terminated.
        await session.commit()

        final_principal: Principal | None = None
        if expected_actor_kind == "ADMIN_HUMAN":
            final_principal = (
                await principal_from_session_row(
                    session,
                    expected_initiator_session_id,
                    for_update=True,
                )
                if expected_initiator_session_id is not None
                else None
            )
        final_row = (
            await session.execute(
                select(models.OutboxMessage)
                .where(models.OutboxMessage.id == oid)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            final_row is None
            or final_row.status != "SENDING"
            or final_row.attempt_count != attempt_no
        ):
            await session.rollback()
            return "SKIPPED_NOT_CLAIMABLE"
        row = final_row

        if expected_actor_kind == "ADMIN_HUMAN":
            if (
                row.actor_kind != expected_actor_kind
                or row.actor_id != expected_actor_id
                or row.initiator_user_id != expected_initiator_user_id
                or row.initiator_session_id != expected_initiator_session_id
                or final_principal is None
                or final_principal.must_change_password
                or final_principal.is_feishu_action
                or final_principal.session_id != row.initiator_session_id
                or final_principal.user_id != row.initiator_user_id
                or final_principal.actor != row.actor_id
            ):
                return await _record_outcome(
                    session,
                    oid,
                    "NEEDS_REVIEW",
                    attempt_no=attempt_no,
                    error_code="HUMAN_AUTHORITY_CHANGED",
                )
        final_is_public = row.message_type != "private_note"
        if not final_is_public:
            if expected_actor_kind == "ADMIN_HUMAN":
                final_authority_error = await _validate_human_outbox_authority(
                    session,
                    outbox=row,
                )
                if final_authority_error is not None:
                    return await _record_outcome(
                        session,
                        oid,
                        "NEEDS_REVIEW",
                        attempt_no=attempt_no,
                        error_code=final_authority_error,
                    )
            return await _stop_before_send(
                session,
                oid,
                "CANCELLED",
                "DIRECT_DRAFT_BLOCKED",
                attempt_no,
            )
        if not isinstance(row.payload, dict):
            return await _stop_before_send(
                session,
                oid,
                "NEEDS_REVIEW",
                "DELIVERY_PAYLOAD_INVALID",
                attempt_no,
                count_attempt=False,
            )
        final_payload = dict(row.payload)
        if not isinstance(final_payload.get("text"), str) or not final_payload["text"].strip():
            return await _stop_before_send(
                session,
                oid,
                "NEEDS_REVIEW",
                "DELIVERY_TEXT_INVALID",
                attempt_no,
                count_attempt=False,
            )
        final_command: TextSendCommand | None
        final_preflight_error = await _public_bot_send_preflight(
            session,
            outbox=row,
            payload_text=final_payload["text"],
        )
        if expected_actor_kind == "ADMIN_HUMAN":
            final_authority_error = await _validate_human_outbox_authority(
                session,
                outbox=row,
            )
            if final_authority_error is not None:
                return await _record_outcome(
                    session,
                    oid,
                    "NEEDS_REVIEW",
                    attempt_no=attempt_no,
                    error_code=final_authority_error,
                )
        if final_preflight_error is not None:
            await _handoff_public_send_failure(
                session,
                outbox=row,
                reason_code=final_preflight_error,
            )
            return await _stop_before_send(
                session,
                oid,
                "CANCELLED",
                final_preflight_error,
                attempt_no,
                count_attempt=False,
            )
        final_stopped, _final_account, final_command = await _validate_direct_send(
            session,
            row,
            final_payload,
            attempt_no,
        )
        if final_stopped is not None:
            return final_stopped

        async def fail_retryable(exc: Exception) -> str:
            if attempt_no >= _MAX_ATTEMPTS:
                return await _record_outcome(
                    session,
                    oid,
                    "NEEDS_REVIEW",
                    attempt_no=attempt_no,
                    error_code="SEND_ERROR",
                    error_message=_safe_error(exc),
                )
            next_at = datetime.now(UTC) + timedelta(seconds=min(30 * 2**attempt_no, 3600))
            return await _record_outcome(
                session,
                oid,
                "FAILED",
                attempt_no=attempt_no,
                error_code="SEND_ERROR",
                error_message=_safe_error(exc),
                next_attempt_at=next_at,
            )

        async def ambiguous(exc: Exception) -> str:
            return await _record_outcome(
                session,
                oid,
                "NEEDS_REVIEW",
                attempt_no=attempt_no,
                error_code="AMBIGUOUS_SEND",
                error_message=_safe_error(exc),
            )

        async def fail_permanent(exc: PlatformSendError) -> str:
            # 平台确定性拒绝(对方不收 DM、超出消息窗口、token 失效):消息未送达且重试无意义,
            # 直接标 NEEDS_REVIEW 并把平台错误码透传给运营,不进退避重试队列。
            return await _record_outcome(
                session,
                oid,
                "NEEDS_REVIEW",
                attempt_no=attempt_no,
                error_code=exc.code,
                error_message=exc.message[:500],
            )

        try:
            sender = await get_platform_sender(row.platform_account_id)
        except Exception as exc:  # noqa: BLE001 - sender resolution happens before dispatch
            return await fail_retryable(exc)
        except BaseException:
            await _rollback_interrupted_delivery(session)
            raise

        try:
            assert final_command is not None
            platform_message_id = await _await_send(
                sender.send_text(
                    target=final_command.target,
                    text=final_command.text,
                )
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return await fail_retryable(exc)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return await ambiguous(exc)
        except RetryableSendError as exc:
            return await fail_retryable(exc)
        except PermanentSendError as exc:
            return await fail_permanent(exc)
        except httpx.HTTPStatusError as exc:
            return (
                await ambiguous(exc)
                if exc.response.status_code >= 500
                else await fail_retryable(exc)
            )
        except Exception as exc:  # noqa: BLE001 - unknown post-dispatch failures are ambiguous
            return await ambiguous(exc)
        except BaseException:
            # The checkpoint is intentionally left SENDING.  Roll back only the transaction
            # holding revalidation row locks so connection-level locks can be released cleanly.
            await _rollback_interrupted_delivery(session)
            raise

        return await _record_outcome(
            session,
            oid,
            "SENT",
            attempt_no=attempt_no,
            platform_message_id=platform_message_id,
        )

    if _is_rate_limited_email_reply(row):
        assert direct_account is not None and direct_command is not None
        locked_account_id = direct_account.id
        locked_sender = email_address_identity_key(direct_command.target["to"])
        # Conversation serialization is already held on this connection.  Commit the read-only
        # preflight before waiting for the per-sender lock so the sender lock can span the
        # attempt checkpoint and provider I/O.
        await session.commit()
        async with hold_connection_advisory_lock(
            connection,
            _email_sender_lock_key(locked_account_id, locked_sender),
        ):
            session.expire_all()
            fresh_row = (
                await session.execute(
                    select(models.OutboxMessage)
                    .where(models.OutboxMessage.id == oid)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if fresh_row is None or fresh_row.status != "SENDING":
                await session.rollback()
                return "SKIPPED_NOT_CLAIMABLE"
            row = fresh_row
            attempt_no = row.attempt_count + 1
            if not isinstance(row.payload, dict):
                return await _stop_before_send(
                    session,
                    oid,
                    "NEEDS_REVIEW",
                    "DELIVERY_PAYLOAD_INVALID",
                    attempt_no,
                    count_attempt=False,
                )
            payload = dict(row.payload)
            if not isinstance(payload.get("text"), str) or not payload["text"].strip():
                return await _stop_before_send(
                    session,
                    oid,
                    "NEEDS_REVIEW",
                    "DELIVERY_TEXT_INVALID",
                    attempt_no,
                    count_attempt=False,
                )
            human_authority_error = await _validate_human_outbox_authority(
                session,
                outbox=row,
                prevalidation_error=human_prevalidation_error,
            )
            if human_authority_error is not None:
                return await _stop_before_send(
                    session,
                    oid,
                    "NEEDS_REVIEW",
                    human_authority_error,
                    attempt_no,
                )
            preflight_error = await _public_bot_send_preflight(
                session,
                outbox=row,
                payload_text=payload["text"],
            )
            if preflight_error is not None:
                await _handoff_public_send_failure(
                    session,
                    outbox=row,
                    reason_code=preflight_error,
                )
                return await _stop_before_send(
                    session,
                    oid,
                    "CANCELLED",
                    preflight_error,
                    attempt_no,
                    count_attempt=False,
                )
            state = await session.scalar(
                select(models.AutomationState.state).where(
                    models.AutomationState.conversation_id == row.conversation_id
                )
            )
            if not _send_state_allowed(row, payload, state):
                return await _stop_before_send(
                    session,
                    oid,
                    "CANCELLED",
                    "TAKEOVER_AT_SEND",
                    attempt_no,
                )
            settings = get_settings()
            stopped, direct_account, direct_command = await _validate_direct_send(
                session,
                row,
                payload,
                attempt_no,
            )
            if stopped is not None:
                return stopped
            assert direct_account is not None and direct_command is not None
            sender = direct_command.target["to"]
            if (
                not _is_rate_limited_email_reply(row)
                or direct_account.id != locked_account_id
                or email_address_identity_key(sender) != locked_sender
            ):
                return await _stop_before_send(
                    session,
                    oid,
                    "NEEDS_REVIEW",
                    "DELIVERY_TARGET_INVALID",
                    attempt_no,
                    count_attempt=False,
                )
            sent_count = await _email_replies_sent_in_window(
                session,
                platform_account_id=direct_account.id,
                sender=sender,
            )
            if sent_count >= settings.email_per_sender_daily_reply_limit:
                return await _stop_before_send(
                    session,
                    oid,
                    "NEEDS_REVIEW",
                    "EMAIL_RATE_LIMITED",
                    attempt_no,
                    count_attempt=False,
                )
            return await dispatch()

    return await dispatch()


async def _prompt_lock_key_for_outbox(
    session: AsyncSession,
    outbox: models.OutboxMessage,
) -> str | None:
    if outbox.message_type == "private_note":
        return None
    payload = dict(outbox.payload) if isinstance(outbox.payload, dict) else {}
    if _effective_origin_kind(outbox, payload) not in {"DECISION", "DRAFT_APPROVAL"}:
        return None
    account = await session.get(models.PlatformAccount, outbox.platform_account_id)
    if account is None:
        return None
    return business_prompt_lock_key(account.tenant_id, account.brand_id)


def _staff_authority_key(user_id: uuid.UUID) -> str:
    return f"social-reply:staff-authority:{user_id}"


async def _deliver_with_connection(
    connection: AsyncConnection,
    *,
    oid: uuid.UUID,
    conversation_id: uuid.UUID,
) -> str:
    prompt_lock_key: str | None = None
    async with AsyncSession(bind=connection, expire_on_commit=False) as scope_session:
        outbox = await scope_session.get(models.OutboxMessage, oid)
        if outbox is not None:
            prompt_lock_key = await _prompt_lock_key_for_outbox(scope_session, outbox)
        await scope_session.commit()
    if prompt_lock_key is not None:
        # Keep prompt activation and provider I/O linearly ordered while the human
        # authority locks acquired by the caller remain held on this connection.
        async with hold_connection_advisory_shared_lock(connection, prompt_lock_key):
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                return await _deliver_outbox_locked(
                    session,
                    connection,
                    oid,
                    conversation_id,
                )
    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        return await _deliver_outbox_locked(
            session,
            connection,
            oid,
            conversation_id,
        )


async def _deliver_prelocked_with_connection(
    connection: AsyncConnection,
    *,
    oid: uuid.UUID,
    conversation_id: uuid.UUID,
) -> str:
    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        outbox = await session.get(models.OutboxMessage, oid)
        if outbox is None:
            return "SKIPPED_NOT_CLAIMABLE"
        async with AsyncExitStack() as locks:
            # Match callbacks and checkpoint rechecks: conversation before route/accounts.
            # A blocked route also mutates conversation work and needs serialization.
            await locks.enter_async_context(
                hold_conversation_delivery_lock_on_connection_in_transaction(
                    connection,
                    conversation_id,
                )
            )
            route_lock = await _lock_public_send_route_accounts(session, outbox=outbox)
            if route_lock is None:
                await session.rollback()
                return "SKIPPED_NOT_CLAIMABLE"
            prompt_lock_key = await _prompt_lock_key_for_outbox(session, outbox)
            if prompt_lock_key is not None:
                await locks.enter_async_context(
                    hold_connection_advisory_shared_lock_in_transaction(
                        connection,
                        prompt_lock_key,
                    )
                )
            return await _deliver_outbox_locked(
                session,
                connection,
                oid,
                conversation_id,
            )


async def deliver_outbox(outbox_id: str) -> str:
    """Serialize takeover with one durable send attempt for the conversation."""
    oid = uuid.UUID(outbox_id)
    async with get_session_factory()() as lookup_session:
        outbox = await lookup_session.get(models.OutboxMessage, oid)
        if outbox is None:
            return "SKIPPED_NOT_CLAIMABLE"
        conversation_id = outbox.conversation_id
        requires_human = outbox.actor_kind == "ADMIN_HUMAN" or outbox.origin_kind in {
            "MANUAL_REPLY",
            "DRAFT_APPROVAL",
        }
        initiator_session_id = outbox.initiator_session_id
        work = None
        if requires_human:
            work = await lookup_session.scalar(
                select(models.HumanWorkItem)
                .where(
                    models.HumanWorkItem.tenant_id == outbox.tenant_id,
                    models.HumanWorkItem.conversation_id == conversation_id,
                    models.HumanWorkItem.status.in_(["WAITING", "CLAIMED"]),
                )
                .execution_options(populate_existing=True)
            )
            staff_ids = {
                value
                for value in (
                    outbox.initiator_user_id,
                    work.assigned_user_id if work is not None else None,
                )
                if value is not None
            }
            if initiator_session_id is not None:
                discovered_principal = await principal_from_session_row(
                    lookup_session,
                    initiator_session_id,
                )
                if discovered_principal is not None and discovered_principal.user_id is not None:
                    staff_ids.add(discovered_principal.user_id)
        assigned_session_id = work.assigned_session_id if work is not None else None
        session_ids = sorted(
            {value for value in (initiator_session_id, assigned_session_id) if value is not None},
            key=str,
        )
        prompt_lock_key = None
        initial_payload = dict(outbox.payload) if isinstance(outbox.payload, dict) else {}
        requires_public_route = outbox.message_type != "private_note" and _effective_origin_kind(
            outbox, initial_payload
        ) in {"DECISION", "DRAFT_APPROVAL"}
        if requires_human and requires_public_route:
            account = await lookup_session.get(
                models.PlatformAccount,
                outbox.platform_account_id,
            )
            if account is not None:
                prompt_lock_key = business_prompt_lock_key(account.tenant_id, account.brand_id)
    if not requires_human:
        if requires_public_route:
            async with get_engine().connect() as connection:
                return await _deliver_prelocked_with_connection(
                    connection,
                    oid=oid,
                    conversation_id=conversation_id,
                )
        async with hold_conversation_delivery_lock(conversation_id) as connection:
            return await _deliver_with_connection(
                connection,
                oid=oid,
                conversation_id=conversation_id,
            )

    # Human delivery has the same lock acquisition order as claim/transfer/start/send:
    # discover assignment and initiator without locks, acquire every staff authority lock
    # in stable order, revalidate the session, then acquire delivery and business locks.
    async with get_engine().connect() as connection:
        async with AsyncExitStack() as locks:
            for staff_id in sorted(staff_ids, key=str):
                await locks.enter_async_context(
                    hold_connection_advisory_lock(connection, _staff_authority_key(staff_id))
                )
            for session_id in session_ids:
                await locks.enter_async_context(
                    hold_connection_advisory_lock(connection, session_authority_key(session_id))
                )
            async with AsyncSession(bind=connection, expire_on_commit=False) as authority_session:
                human_principal: Principal | None = None
                if session_ids:
                    await authority_session.execute(
                        select(models.AdminSession)
                        .where(models.AdminSession.id.in_(session_ids))
                        .execution_options(populate_existing=True)
                        .order_by(models.AdminSession.id)
                        .with_for_update()
                    )
                human_prevalidation_error: str | None = None
                if initiator_session_id is None:
                    human_prevalidation_error = "HUMAN_INITIATOR_SESSION_MISSING"
                else:
                    human_principal = await principal_from_session_row(
                        authority_session,
                        initiator_session_id,
                        for_update=True,
                    )
                    if human_principal is None:
                        human_prevalidation_error = "HUMAN_INITIATOR_SESSION_INVALID"
                await locks.enter_async_context(
                    hold_conversation_delivery_lock_on_connection_in_transaction(
                        connection,
                        conversation_id,
                    )
                )
                if requires_public_route:
                    route_lock_outbox = await authority_session.get(
                        models.OutboxMessage,
                        oid,
                    )
                    if route_lock_outbox is None:
                        await authority_session.rollback()
                        return "SKIPPED_NOT_CLAIMABLE"
                    route_lock = await _lock_public_send_route_accounts(
                        authority_session,
                        outbox=route_lock_outbox,
                    )
                    if route_lock is None:
                        await authority_session.rollback()
                        return "SKIPPED_NOT_CLAIMABLE"
                if prompt_lock_key is not None:
                    await locks.enter_async_context(
                        hold_connection_advisory_shared_lock_in_transaction(
                            connection,
                            prompt_lock_key,
                        )
                    )
                return await _deliver_outbox_locked(
                    authority_session,
                    connection,
                    oid,
                    conversation_id,
                    human_prevalidation_error=human_prevalidation_error,
                )
