import time

from fastapi import APIRouter, Request, Response
from sqlalchemy import insert

from social_reply.application.event_ingestion.actors import process_chatwoot_event
from social_reply.connectors.chatwoot.signature import verify_signature
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

router = APIRouter()


def _recover_message_created(payload: dict) -> dict | None:
    """部分 Chatwoot 部署偶发漏投 message_created，但后续 conversation_* 事件携带最新消息。

    将 conversation.messages[-1] 恢复为标准 message_created 形态，交给现有幂等链路处理；
    NormalizedEvent 的 message id 唯一键保证同一消息之后若补投原事件也不会重复回复。
    """
    if payload.get("event") not in {
        "conversation_updated",
        "conversation_typing_on",
        "conversation_typing_off",
    }:
        return None
    # Chatwoot 的 conversation_* webhook 有两种形态：
    # 1) {event, conversation: {...}}；2) 会话字段直接位于 payload 顶层。
    conversation = payload.get("conversation") or payload
    messages = conversation.get("messages") or []
    if not messages:
        return None
    message = messages[-1]
    if not isinstance(message, dict) or "id" not in message:
        return None
    recovered = dict(message)
    recovered["event"] = "message_created"
    recovered["conversation"] = {
        "id": message.get("conversation_id") or conversation.get("id"),
        "inbox_id": message.get("inbox_id") or conversation.get("inbox_id"),
        "status": conversation.get("status"),
    }
    recovered["account"] = {
        "id": message.get("account_id") or (conversation.get("account") or {}).get("id", 0)
    }
    recovered["sender"] = message.get("sender") or {}
    recovered["created_at"] = message.get("created_at")
    return recovered


@router.post("/webhooks/chatwoot")
async def chatwoot_webhook(request: Request) -> Response:
    settings = get_settings()
    body = await request.body()
    ok = verify_signature(
        secret=settings.chatwoot_webhook_secret,
        timestamp=request.headers.get("X-Chatwoot-Timestamp", ""),
        body=body,
        signature=request.headers.get("X-Chatwoot-Signature", ""),
        now=time.time(),
        tolerance=settings.chatwoot_signature_tolerance_seconds,
    )
    if not ok:
        return Response(status_code=401)

    payload = await request.json()
    is_message = payload.get("event") == "message_created"
    if not is_message:
        recovered = _recover_message_created(payload)
        if recovered is not None:
            payload = recovered
            is_message = True
    async with get_session_factory()() as session:
        result = await session.execute(
            insert(models.RawEvent)
            .values(
                source="chatwoot",
                payload=payload,
                headers={
                    "X-Chatwoot-Timestamp": request.headers.get("X-Chatwoot-Timestamp"),
                    "X-Chatwoot-Delivery": request.headers.get("X-Chatwoot-Delivery"),
                    "User-Agent": request.headers.get("User-Agent"),
                },
                processing_status="PENDING" if is_message else "IGNORED_AT_INGRESS",
            )
            .returning(models.RawEvent.id)
        )
        raw_event_id = result.scalar_one()
        await session.commit()

    # 入口只做验签+存 raw+入队，重活交给 worker
    if is_message:
        process_chatwoot_event.send(str(raw_event_id))
    return Response(status_code=200)
