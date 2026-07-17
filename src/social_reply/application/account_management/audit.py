import logging

from sqlalchemy import insert

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

logger = logging.getLogger(__name__)


async def record_account_management_audit(
    *,
    tenant_id: str,
    actor: str,
    action: str,
    subject_id: str,
    detail: dict,
) -> None:
    """记录控制面操作；detail 只能放非敏感元数据，禁止写 token/secret。"""
    try:
        async with get_session_factory()() as session:
            await session.execute(
                insert(models.AuditLog).values(
                    tenant_id=tenant_id,
                    category="account_management",
                    actor=actor,
                    action=action,
                    subject_type="platform_account",
                    subject_id=subject_id,
                    detail=detail,
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - 账号已成功连接，审计降级到结构化进程日志
        logger.exception(
            "account management audit persistence failed",
            extra={
                "tenant_id": tenant_id,
                "actor": actor,
                "action": action,
                "subject_id": subject_id,
                "detail": detail,
            },
        )
