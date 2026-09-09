"""Read-only audit scope without disclosing unrelated workspace events."""

from sqlalchemy import String, and_, cast, or_, select

from social_reply.application.account_management.access import account_read_condition
from social_reply.application.account_management.auth import Principal
from social_reply.infrastructure.database import models


def audit_read_condition(principal: Principal, tenant_id: str):
    principal.require_tenant(tenant_id)
    principal.require_capability("audit.read")
    tenant_condition = models.AuditLog.tenant_id == tenant_id
    if principal.is_workspace_admin:
        return tenant_condition
    visible_accounts = select(models.PlatformAccount.id).where(
        account_read_condition(principal, tenant_id)
    )
    visible_conversations = select(models.Conversation.id).where(
        models.Conversation.tenant_id == tenant_id,
        models.Conversation.platform_account_id.in_(visible_accounts),
    )
    scoped_subjects = (
        (
            ("platform_account",),
            select(cast(models.PlatformAccount.id, String)).where(
                models.PlatformAccount.id.in_(visible_accounts)
            ),
        ),
        (
            ("conversation",),
            select(cast(models.Conversation.id, String)).where(
                models.Conversation.id.in_(visible_conversations)
            ),
        ),
        (
            ("human_work_item",),
            select(cast(models.HumanWorkItem.id, String)).where(
                models.HumanWorkItem.tenant_id == tenant_id,
                models.HumanWorkItem.conversation_id.in_(visible_conversations),
            ),
        ),
        (
            ("outbox", "outbox_message"),
            select(cast(models.OutboxMessage.id, String)).where(
                models.OutboxMessage.tenant_id == tenant_id,
                models.OutboxMessage.platform_account_id.in_(visible_accounts),
            ),
        ),
    )
    return and_(
        tenant_condition,
        or_(
            *(
                and_(
                    models.AuditLog.subject_type.in_(subject_types),
                    models.AuditLog.subject_id.in_(subject_ids),
                )
                for subject_types, subject_ids in scoped_subjects
            )
        ),
    )


def auditor_safe_detail(detail: dict | None) -> dict:
    # Freeform audit payloads may contain identifiers for other accounts or staff.
    allowed_keys = frozenset({"status", "previous_status", "outcome", "enabled"})
    return {
        key: value
        for key, value in (detail or {}).items()
        if key in allowed_keys and isinstance(value, (str, bool, int, type(None)))
    }
