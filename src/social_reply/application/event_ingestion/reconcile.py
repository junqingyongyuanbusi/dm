import logging

import httpx
from sqlalchemy import select

from social_reply.application.event_ingestion.actors import process_chatwoot_event
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)


async def reconcile_chatwoot_messages() -> list[str]:
    """从已知 Chatwoot 会话补拉漏投的 incoming 消息，返回新建 RawEvent id。

    Chatwoot 某些部署偶发不投 message_created；补拉依赖现有 normalized_events
    唯一键去重，因此与迟到 webhook 并发也不会重复回复。
    """
    settings = get_settings()
    async with get_session_factory()() as session:
        mappings = (
            await session.execute(
                select(
                    models.ConversationMapping.chatwoot_account_id,
                    models.ConversationMapping.chatwoot_conversation_id,
                )
            )
        ).all()

    created: list[str] = []
    headers = {"api_access_token": settings.chatwoot_api_token}
    async with httpx.AsyncClient(
        base_url=settings.chatwoot_base_url,
        headers=headers,
        timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=2.0),
    ) as client:
        for account_id, conversation_id in mappings:
            try:
                response = await client.get(
                    f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
                )
                response.raise_for_status()
                messages = response.json().get("payload", [])[-10:]
            except Exception:
                logger.warning(
                    "chatwoot reconcile fetch failed account=%s conversation=%s",
                    account_id,
                    conversation_id,
                    exc_info=True,
                )
                continue

            for message in messages:
                message_type = message.get("message_type")
                if message_type not in (0, "incoming") or message.get("private"):
                    continue
                external_id = str(message.get("id"))
                async with get_session_factory()() as session:
                    exists = (
                        await session.execute(
                            select(models.NormalizedEvent.id).where(
                                models.NormalizedEvent.external_event_id == external_id
                            )
                        )
                    ).first()
                    if exists is not None:
                        continue
                    payload = dict(message)
                    payload.update(
                        event="message_created",
                        conversation={
                            "id": message.get("conversation_id") or conversation_id,
                            "inbox_id": message.get("inbox_id"),
                            "status": "open",
                        },
                        account={"id": message.get("account_id") or account_id},
                        sender=message.get("sender") or {},
                    )
                    raw_id = (
                        await session.execute(
                            models.RawEvent.__table__.insert()
                            .values(
                                source="chatwoot_reconcile",
                                payload=payload,
                                headers={},
                                processing_status="PENDING",
                            )
                            .returning(models.RawEvent.id)
                        )
                    ).scalar_one()
                    await session.commit()
                process_chatwoot_event.send(str(raw_id))
                created.append(str(raw_id))
    return created
