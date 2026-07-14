import time

from fastapi import APIRouter, Request, Response
from sqlalchemy import insert

from social_reply.application.event_ingestion.actors import process_chatwoot_event
from social_reply.connectors.chatwoot.signature import verify_signature
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

router = APIRouter()


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
    async with get_session_factory()() as session:
        result = await session.execute(
            insert(models.RawEvent)
            .values(source="chatwoot", payload=payload,
                    headers={"X-Chatwoot-Timestamp": request.headers.get("X-Chatwoot-Timestamp")})
            .returning(models.RawEvent.id)
        )
        raw_event_id = result.scalar_one()
        await session.commit()

    # PLAN.md §四：入口只做验签+存 raw+入队，重活交给 worker
    if payload.get("event") == "message_created":
        process_chatwoot_event.send(str(raw_event_id))
    return Response(status_code=200)
