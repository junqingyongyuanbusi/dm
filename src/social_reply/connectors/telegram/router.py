from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import insert

from social_reply.application.event_ingestion.direct_actors import process_direct_event
from social_reply.application.platform_accounts import find_platform_account_by_public_id
from social_reply.connectors.telegram.adapter import TelegramWebhookAdapter
from social_reply.domain.messages.canonical import canonical_event_to_dict
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor

router = APIRouter()


@router.post("/webhooks/telegram/{public_id}")
async def telegram_webhook(public_id: str, request: Request) -> Response:
    account = await find_platform_account_by_public_id(platform="telegram", public_id=public_id)
    if account is None:
        raise HTTPException(status_code=404, detail="telegram_bot_not_found")
    body = await request.body()
    adapter = TelegramWebhookAdapter(
        account_id=str(account.id),
        secret=account.webhook_secret,
    )
    headers = {key.lower(): value for key, value in request.headers.items()}
    if not adapter.verify(headers=headers, body=body):
        raise HTTPException(status_code=401, detail="invalid_telegram_webhook_secret")
    payload = await request.json()
    events = adapter.normalize(payload)
    async with get_session_factory()() as session:
        raw_event_id = (
            await session.execute(
                insert(models.RawEvent)
                .values(
                    source="telegram",
                    payload=payload,
                    headers={"secret_verified": True},
                    processing_status="PENDING" if events else "IGNORED_AT_INGRESS",
                )
                .returning(models.RawEvent.id)
            )
        ).scalar_one()
        await session.commit()
    serialized_events = [canonical_event_to_dict(event) for event in events]
    if serialized_events:
        from social_reply.application.event_ingestion.direct_actors import _process_events

        await dispatch_actor(
            process_direct_event,
            str(raw_event_id),
            serialized_events,
            inline=lambda: _process_events(raw_event_id, serialized_events),
        )
    return Response(status_code=200)
