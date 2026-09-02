import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from social_reply.connectors.errors import PermanentSendError, RetryableSendError
from social_reply.connectors.feishu.client import FeishuClient, FeishuClientError
from social_reply.connectors.registry import get_platform_sender
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)


class FeishuHandoffError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FeishuHandoffNotFound(FeishuHandoffError):
    pass


class FeishuHandoffConflict(FeishuHandoffError):
    pass


class FeishuHandoffValidationError(FeishuHandoffError):
    pass


@dataclass(frozen=True)
class FeishuHandoffSnapshot:
    accounts: tuple[models.PlatformAccount, ...]
    config: models.TenantFeishuHandoffConfig | None
    operators: tuple[models.FeishuHandoffOperator, ...]
    failures: tuple[models.HandoffNotificationIntent, ...]


async def load_feishu_handoff_snapshot(tenant_id: str) -> FeishuHandoffSnapshot:
    async with get_session_factory()() as session:
        accounts = tuple(
            (
                await session.execute(
                    select(models.PlatformAccount)
                    .where(
                        models.PlatformAccount.tenant_id == tenant_id,
                        models.PlatformAccount.platform == "feishu",
                        models.PlatformAccount.status == "active",
                    )
                    .order_by(models.PlatformAccount.name)
                )
            ).scalars()
        )
        config = await session.scalar(
            select(models.TenantFeishuHandoffConfig).where(
                models.TenantFeishuHandoffConfig.tenant_id == tenant_id
            )
        )
        operators = tuple(
            (
                await session.execute(
                    select(models.FeishuHandoffOperator)
                    .where(models.FeishuHandoffOperator.tenant_id == tenant_id)
                    .order_by(models.FeishuHandoffOperator.created_at)
                )
            ).scalars()
        )
        failures = tuple(
            (
                await session.execute(
                    select(models.HandoffNotificationIntent)
                    .where(
                        models.HandoffNotificationIntent.tenant_id == tenant_id,
                        models.HandoffNotificationIntent.status.in_(
                            ("BLOCKED_CONFIG", "FAILED", "NEEDS_REVIEW")
                        ),
                    )
                    .order_by(models.HandoffNotificationIntent.updated_at.desc())
                    .limit(50)
                )
            ).scalars()
        )
    return FeishuHandoffSnapshot(
        accounts=accounts,
        config=config,
        operators=operators,
        failures=failures,
    )


async def _lock_config(session, tenant_id: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"social-reply:feishu-handoff-config:{tenant_id}"},
    )


async def save_feishu_handoff_config(
    *,
    tenant_id: str,
    actor: str,
    account_id: uuid.UUID,
    destination_chat_id: str,
    enabled: bool,
) -> uuid.UUID:
    chat_id = destination_chat_id.strip()
    if not chat_id or len(chat_id) > 256:
        raise FeishuHandoffValidationError("invalid_feishu_chat_id")
    async with get_session_factory()() as session:
        await _lock_config(session, tenant_id)
        account = await session.scalar(
            select(models.PlatformAccount)
            .where(models.PlatformAccount.id == account_id)
            .with_for_update()
        )
        if (
            account is None
            or account.tenant_id != tenant_id
            or account.platform != "feishu"
            or account.status != "active"
        ):
            raise FeishuHandoffNotFound("feishu_account_not_found")
        config = await session.scalar(
            select(models.TenantFeishuHandoffConfig)
            .where(models.TenantFeishuHandoffConfig.tenant_id == tenant_id)
            .with_for_update()
        )
        previous = None
        if config is None:
            config = models.TenantFeishuHandoffConfig(
                tenant_id=tenant_id,
                feishu_platform_account_id=account_id,
                destination_chat_id=chat_id,
                enabled=enabled,
                config_version=1,
            )
            session.add(config)
        else:
            previous = {
                "account_id": str(config.feishu_platform_account_id),
                "chat_id": config.destination_chat_id,
                "enabled": config.enabled,
                "config_version": config.config_version,
            }
            changed = (
                config.feishu_platform_account_id != account_id
                or config.destination_chat_id != chat_id
                or config.enabled != enabled
            )
            config.feishu_platform_account_id = account_id
            config.destination_chat_id = chat_id
            config.enabled = enabled
            if changed:
                config.config_version += 1
        await session.flush()
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=actor,
                action="SET_FEISHU_HANDOFF_CONFIG",
                subject_type="tenant_feishu_handoff_config",
                subject_id=str(config.id),
                detail={
                    "previous": previous,
                    "account_id": str(account_id),
                    "chat_id": chat_id,
                    "enabled": enabled,
                    "config_version": config.config_version,
                },
            )
        )
        await session.commit()
        return config.id


async def upsert_feishu_handoff_operator(
    *,
    tenant_id: str,
    actor: str,
    operator_open_id: str,
    display_name: str,
    can_claim: bool,
    can_resolve: bool,
) -> uuid.UUID:
    open_id = operator_open_id.strip()
    normalized_display_name = display_name.strip()
    if not open_id or len(open_id) > 128:
        raise FeishuHandoffValidationError("invalid_operator_open_id")
    if len(normalized_display_name) > 100 or not (can_claim or can_resolve):
        raise FeishuHandoffValidationError("invalid_operator_permissions")
    async with get_session_factory()() as session:
        await _lock_config(session, tenant_id)
        config = await session.scalar(
            select(models.TenantFeishuHandoffConfig)
            .where(models.TenantFeishuHandoffConfig.tenant_id == tenant_id)
            .with_for_update()
        )
        if config is None:
            raise FeishuHandoffConflict("feishu_handoff_config_required")
        operator_id = uuid.uuid4()
        saved_id = (
            await session.execute(
                pg_insert(models.FeishuHandoffOperator)
                .values(
                    id=operator_id,
                    tenant_id=tenant_id,
                    feishu_platform_account_id=config.feishu_platform_account_id,
                    operator_open_id=open_id,
                    display_name=normalized_display_name or None,
                    can_claim=can_claim,
                    can_resolve=can_resolve,
                    status="ACTIVE",
                )
                .on_conflict_do_update(
                    index_elements=["feishu_platform_account_id", "operator_open_id"],
                    set_={
                        "display_name": normalized_display_name or None,
                        "can_claim": can_claim,
                        "can_resolve": can_resolve,
                        "status": "ACTIVE",
                    },
                )
                .returning(models.FeishuHandoffOperator.id)
            )
        ).scalar_one()
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=actor,
                action="SET_FEISHU_HANDOFF_OPERATOR",
                subject_type="feishu_handoff_operator",
                subject_id=str(saved_id),
                detail={
                    "open_id": open_id,
                    "can_claim": can_claim,
                    "can_resolve": can_resolve,
                },
            )
        )
        await session.commit()
        return saved_id


async def set_feishu_handoff_operator_status(
    *,
    tenant_id: str,
    actor: str,
    operator_id: uuid.UUID,
    enabled: bool,
) -> None:
    target_status = "ACTIVE" if enabled else "DISABLED"
    async with get_session_factory()() as session:
        operator = (
            await session.execute(
                select(models.FeishuHandoffOperator)
                .where(
                    models.FeishuHandoffOperator.id == operator_id,
                    models.FeishuHandoffOperator.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operator is None:
            raise FeishuHandoffNotFound("feishu_handoff_operator_not_found")
        previous_status = operator.status
        operator.status = target_status
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=actor,
                action="SET_FEISHU_HANDOFF_OPERATOR_STATUS",
                subject_type="feishu_handoff_operator",
                subject_id=str(operator.id),
                detail={
                    "from": previous_status,
                    "to": target_status,
                    "enabled": enabled,
                    "changed": previous_status != target_status,
                },
            )
        )
        await session.commit()


async def send_feishu_handoff_test_card(
    *,
    tenant_id: str,
    actor: str,
    title: str,
    content: str,
    sender_factory: Callable[[uuid.UUID], Awaitable[object]] = get_platform_sender,
) -> str:
    async with get_session_factory()() as session:
        config = await session.scalar(
            select(models.TenantFeishuHandoffConfig).where(
                models.TenantFeishuHandoffConfig.tenant_id == tenant_id
            )
        )
        account = (
            await session.get(models.PlatformAccount, config.feishu_platform_account_id)
            if config is not None
            else None
        )
    if (
        not get_settings().feishu_enabled
        or config is None
        or not config.enabled
        or account is None
        or account.tenant_id != tenant_id
        or account.platform != "feishu"
        or str((account.config or {}).get("feishu_health_status") or "") != "READY"
    ):
        return "test_failed"
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }
    outcome = "test_failed"
    provider_message_id = None
    try:
        sender = await sender_factory(account.id)
        if not isinstance(sender, FeishuClient):
            raise PermanentSendError("FEISHU_HANDOFF_SENDER_INVALID")
        provider_message_id = await sender.create_interactive_card(
            chat_id=config.destination_chat_id,
            card=card,
            provider_uuid=str(uuid.uuid4()),
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, PermanentSendError, RetryableSendError):
        provider_message_id = None
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError, FeishuClientError):
        outcome = "test_ambiguous"
        provider_message_id = None
    except Exception as exc:  # noqa: BLE001 - unknown outcomes must never be retried blindly
        logger.exception(
            "Unexpected Feishu handoff test-card result tenant_id=%s error_type=%s",
            tenant_id,
            type(exc).__name__,
        )
        outcome = "test_ambiguous"
        provider_message_id = None
    else:
        outcome = "test_sent"
    async with get_session_factory()() as session:
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=actor,
                action="SEND_FEISHU_HANDOFF_TEST_CARD",
                subject_type="tenant_feishu_handoff_config",
                subject_id=str(config.id),
                detail={
                    "outcome": outcome,
                    "provider_message_id": provider_message_id,
                },
            )
        )
        await session.commit()
    return outcome
