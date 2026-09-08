from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from sqlalchemy import select, text

from social_reply.application.account_management.access import lock_user_authority
from social_reply.application.account_management.auth import (
    Principal,
    authenticated_principal_context,
    principal_from_session_row,
)
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
    staff: tuple[models.AdminUser, ...] = ()


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
        staff = tuple(
            await session.scalars(
                select(models.AdminUser)
                .where(
                    models.AdminUser.tenant_id == tenant_id,
                    models.AdminUser.status == "active",
                    models.AdminUser.role.in_(("USER", "WORKSPACE_ADMIN")),
                )
                .order_by(models.AdminUser.username)
            )
        )
    return FeishuHandoffSnapshot(
        accounts=accounts,
        config=config,
        operators=operators,
        failures=failures,
        staff=staff,
    )


async def _lock_config(session, tenant_id: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"social-reply:feishu-handoff-config:{tenant_id}"},
    )


async def _require_handoff_admin(
    session,
    *,
    principal: Principal | None,
    tenant_id: str,
    account: models.PlatformAccount | None = None,
    staff_user_ids: tuple[uuid.UUID, ...] = (),
) -> Principal:
    principal = principal or authenticated_principal_context()
    if principal is None:
        raise FeishuHandoffConflict("feishu_handoff_principal_required")
    staff_ids = set(staff_user_ids)
    if principal.user_id is not None:
        staff_ids.add(principal.user_id)
    for staff_id in sorted(staff_ids, key=str):
        await lock_user_authority(session, staff_id)
    if principal.session_id is None or principal.is_feishu_action:
        raise FeishuHandoffConflict("feishu_handoff_principal_required")
    current = await principal_from_session_row(session, principal.session_id, for_update=True)
    if (
        current is None
        or current.must_change_password
        or current.user_id != principal.user_id
        or not current.is_workspace_admin
    ):
        raise FeishuHandoffConflict("feishu_handoff_admin_required")
    if tenant_id not in current.allowed_tenants or current.tenant_id not in {None, tenant_id}:
        raise FeishuHandoffNotFound("feishu_handoff_tenant_not_found")
    if account is not None and not current.can_access_account(account):
        raise FeishuHandoffNotFound("feishu_account_not_found")
    return current


async def _require_staff_binding(
    session,
    *,
    tenant_id: str,
    admin_user_id: uuid.UUID | None,
) -> models.AdminUser:
    if admin_user_id is None:
        raise FeishuHandoffValidationError("feishu_operator_staff_required")
    user = await session.scalar(
        select(models.AdminUser)
        .where(
            models.AdminUser.id == admin_user_id,
            models.AdminUser.tenant_id == tenant_id,
            models.AdminUser.status == "active",
            models.AdminUser.role.in_(["USER", "WORKSPACE_ADMIN"]),
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if user is None:
        raise FeishuHandoffValidationError("feishu_operator_staff_not_accessible")
    return user


async def save_feishu_handoff_config(
    *,
    tenant_id: str,
    actor: str,
    account_id: uuid.UUID,
    destination_chat_id: str,
    enabled: bool,
    principal: Principal | None = None,
) -> uuid.UUID:
    chat_id = destination_chat_id.strip()
    if not chat_id or len(chat_id) > 256:
        raise FeishuHandoffValidationError("invalid_feishu_chat_id")
    async with get_session_factory()() as session:
        current_admin = await _require_handoff_admin(
            session,
            principal=principal,
            tenant_id=tenant_id,
        )
        await _lock_config(session, tenant_id)
        config = await session.scalar(
            select(models.TenantFeishuHandoffConfig)
            .where(models.TenantFeishuHandoffConfig.tenant_id == tenant_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        account = await session.scalar(
            select(models.PlatformAccount)
            .where(
                models.PlatformAccount.id == account_id,
                models.PlatformAccount.tenant_id == tenant_id,
                models.PlatformAccount.platform == "feishu",
                models.PlatformAccount.status == "active",
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if account is None or not current_admin.can_access_account(account):
            raise FeishuHandoffNotFound("feishu_account_not_found")
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
                actor=current_admin.actor,
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
    admin_user_id: uuid.UUID | None = None,
    principal: Principal | None = None,
    confirm_rebind: bool = False,
    expected_admin_user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    open_id = operator_open_id.strip()
    normalized_display_name = display_name.strip()
    if not open_id or len(open_id) > 128:
        raise FeishuHandoffValidationError("invalid_operator_open_id")
    if len(normalized_display_name) > 100 or not (can_claim or can_resolve):
        raise FeishuHandoffValidationError("invalid_operator_permissions")
    supplied = principal or authenticated_principal_context()
    if supplied is None:
        raise FeishuHandoffConflict("feishu_handoff_principal_required")
    async with get_session_factory()() as session:
        snapshot = (
            await session.execute(
                select(
                    models.TenantFeishuHandoffConfig.id,
                    models.TenantFeishuHandoffConfig.feishu_platform_account_id,
                    models.TenantFeishuHandoffConfig.config_version,
                ).where(models.TenantFeishuHandoffConfig.tenant_id == tenant_id)
            )
        ).one_or_none()
        if snapshot is None:
            raise FeishuHandoffConflict("feishu_handoff_config_required")
        config_id, bot_id, config_version = snapshot
        old = (
            await session.execute(
                select(
                    models.FeishuHandoffOperator.id, models.FeishuHandoffOperator.admin_user_id
                ).where(
                    models.FeishuHandoffOperator.tenant_id == tenant_id,
                    models.FeishuHandoffOperator.feishu_platform_account_id == bot_id,
                    models.FeishuHandoffOperator.operator_open_id == open_id,
                )
            )
        ).one_or_none()
        previous_user_id = old.admin_user_id if old is not None else None
        binding_user_id = admin_user_id if admin_user_id is not None else previous_user_id
        if binding_user_id is None:
            raise FeishuHandoffValidationError("feishu_operator_staff_required")
        affected = tuple(
            {value for value in (previous_user_id, binding_user_id) if value is not None}
        )
        current_admin = await _require_handoff_admin(
            session,
            principal=supplied,
            tenant_id=tenant_id,
            staff_user_ids=affected,
        )
        staff = await _require_staff_binding(
            session,
            tenant_id=tenant_id,
            admin_user_id=binding_user_id,
        )
        await _lock_config(session, tenant_id)
        account = await session.scalar(
            select(models.PlatformAccount)
            .where(
                models.PlatformAccount.id == bot_id,
                models.PlatformAccount.tenant_id == tenant_id,
                models.PlatformAccount.platform == "feishu",
                models.PlatformAccount.status == "active",
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        config = await session.scalar(
            select(models.TenantFeishuHandoffConfig)
            .where(models.TenantFeishuHandoffConfig.id == config_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            account is None
            or config is None
            or config.config_version != config_version
            or config.feishu_platform_account_id != bot_id
        ):
            raise FeishuHandoffConflict("feishu_handoff_config_changed")
        operator = await session.scalar(
            select(models.FeishuHandoffOperator)
            .where(
                models.FeishuHandoffOperator.tenant_id == tenant_id,
                models.FeishuHandoffOperator.feishu_platform_account_id == bot_id,
                models.FeishuHandoffOperator.operator_open_id == open_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (old is None) != (operator is None) or (
            operator is not None
            and (operator.id != old.id or operator.admin_user_id != previous_user_id)
        ):
            raise FeishuHandoffConflict("feishu_operator_binding_changed")
        rebound = previous_user_id is not None and previous_user_id != staff.id
        if rebound and (not confirm_rebind or expected_admin_user_id != previous_user_id):
            raise FeishuHandoffConflict("feishu_operator_rebind_confirmation_required")
        if operator is None:
            operator = models.FeishuHandoffOperator(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                feishu_platform_account_id=bot_id,
                operator_open_id=open_id,
                admin_user_id=staff.id,
                status="ACTIVE",
                can_claim=can_claim,
                can_resolve=can_resolve,
            )
            session.add(operator)
        operator.display_name = normalized_display_name or None
        operator.admin_user_id = staff.id
        operator.can_claim = can_claim
        operator.can_resolve = can_resolve
        await session.flush()
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=current_admin.actor,
                action="REBIND_FEISHU_HANDOFF_OPERATOR"
                if rebound
                else "SET_FEISHU_HANDOFF_OPERATOR",
                subject_type="feishu_handoff_operator",
                subject_id=str(operator.id),
                detail={
                    "open_id": open_id,
                    "can_claim": can_claim,
                    "can_resolve": can_resolve,
                    "previous_admin_user_id": str(previous_user_id) if previous_user_id else None,
                    "admin_user_id": str(staff.id),
                    "rebind_confirmed": rebound and confirm_rebind,
                    "actor_user_id": str(current_admin.user_id) if current_admin.user_id else None,
                    "actor_session_id": str(current_admin.session_id),
                },
            )
        )
        await session.commit()
        return operator.id


async def set_feishu_handoff_operator_status(
    *,
    tenant_id: str,
    actor: str,
    operator_id: uuid.UUID,
    enabled: bool,
    principal: Principal | None = None,
) -> None:
    target_status = "ACTIVE" if enabled else "DISABLED"
    async with get_session_factory()() as session:
        operator_identity = await session.scalar(
            select(models.FeishuHandoffOperator)
            .where(
                models.FeishuHandoffOperator.id == operator_id,
                models.FeishuHandoffOperator.tenant_id == tenant_id,
            )
            .execution_options(populate_existing=True)
        )
        if operator_identity is None:
            raise FeishuHandoffNotFound("feishu_handoff_operator_not_found")
        identity_account_id = operator_identity.feishu_platform_account_id
        identity_user_id = operator_identity.admin_user_id
        current_admin = await _require_handoff_admin(
            session,
            principal=principal,
            tenant_id=tenant_id,
            staff_user_ids=(identity_user_id,) if identity_user_id is not None else (),
        )
        account = await session.scalar(
            select(models.PlatformAccount)
            .where(
                models.PlatformAccount.id == identity_account_id,
                models.PlatformAccount.tenant_id == tenant_id,
                models.PlatformAccount.platform == "feishu",
                models.PlatformAccount.status == "active",
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        operator = await session.scalar(
            select(models.FeishuHandoffOperator)
            .where(
                models.FeishuHandoffOperator.id == operator_id,
                models.FeishuHandoffOperator.tenant_id == tenant_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            account is None
            or operator is None
            or operator.feishu_platform_account_id != identity_account_id
            or operator.admin_user_id != identity_user_id
            or not current_admin.can_access_account(account)
        ):
            raise FeishuHandoffNotFound("feishu_account_not_found")
        if enabled:
            if operator.admin_user_id is None:
                raise FeishuHandoffConflict("feishu_operator_staff_required")
            await _require_staff_binding(
                session,
                tenant_id=tenant_id,
                admin_user_id=operator.admin_user_id,
            )
        previous_status = operator.status
        operator.status = target_status
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=current_admin.actor,
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
    principal: Principal | None = None,
) -> str:
    async with get_session_factory()() as session:
        current_admin = await _require_handoff_admin(
            session,
            principal=principal,
            tenant_id=tenant_id,
        )
        await _lock_config(session, tenant_id)
        config = await session.scalar(
            select(models.TenantFeishuHandoffConfig)
            .where(models.TenantFeishuHandoffConfig.tenant_id == tenant_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if config is None:
            return "test_failed"
        account = await session.scalar(
            select(models.PlatformAccount)
            .where(
                models.PlatformAccount.id == config.feishu_platform_account_id,
                models.PlatformAccount.tenant_id == tenant_id,
                models.PlatformAccount.platform == "feishu",
                models.PlatformAccount.status == "active",
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if account is None or not current_admin.can_access_account(account):
            return "test_failed"
        if (
            not get_settings().feishu_enabled
            or not config.enabled
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
        except (
            httpx.TimeoutException,
            httpx.TransportError,
            httpx.HTTPStatusError,
            FeishuClientError,
        ):
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
        session.add(
            models.AuditLog(
                tenant_id=tenant_id,
                category="admin_action",
                actor=current_admin.actor,
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
