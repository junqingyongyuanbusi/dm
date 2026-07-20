import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import insert

from social_reply.application.event_ingestion.direct_actors import process_direct_event
from social_reply.application.platform_accounts import find_platform_account_by_public_id
from social_reply.connectors.x.adapter import XWebhookAdapter
from social_reply.connectors.x.signature import crc_response, verify_x_signature
from social_reply.domain.messages.canonical import canonical_event_to_dict
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/webhooks/x/{public_id}")
async def x_crc(public_id: str, crc_token: str = Query()) -> dict[str, str]:
    account = await find_platform_account_by_public_id(platform="x", public_id=public_id)
    if account is None:
        raise HTTPException(status_code=404, detail="x_account_not_found")
    return {
        "response_token": crc_response(
            consumer_secret=account.webhook_secret_bundle["consumer_secret"],
            crc_token=crc_token,
        )
    }


@router.post("/webhooks/x/{public_id}")
async def x_webhook(public_id: str, request: Request) -> dict[str, bool]:
    account = await find_platform_account_by_public_id(platform="x", public_id=public_id)
    if account is None:
        raise HTTPException(status_code=404, detail="x_account_not_found")
    body = await request.body()
    if not verify_x_signature(
        consumer_secret=account.webhook_secret_bundle["consumer_secret"],
        body=body,
        signature=request.headers.get("X-Twitter-Webhooks-Signature"),
    ):
        raise HTTPException(status_code=401, detail="invalid_x_signature")
    payload = json.loads(body)
    event_type = str((payload.get("data") or {}).get("event_type") or "")
    is_xchat = event_type.startswith("chat.")
    adapter = XWebhookAdapter(
        account_id=str(account.id),
        external_account_id=account.external_account_id,
    )
    events = adapter.normalize(payload)
    processing_status = (
        "XCHAT_DECRYPTION_PENDING"
        if is_xchat
        else "PENDING"
        if events
        else "IGNORED_AT_INGRESS"
    )
    async with get_session_factory()() as session:
        raw_event_id = (
            await session.execute(
                insert(models.RawEvent)
                .values(
                    source="x",
                    payload=payload,
                    headers={
                        "signature_verified": True,
                        "event_type": event_type or None,
                    },
                    processing_status=processing_status,
                )
                .returning(models.RawEvent.id)
            )
        ).scalar_one()
        if is_xchat:
            await session.execute(
                insert(models.AuditLog).values(
                    tenant_id=account.tenant_id,
                    category="ingestion",
                    actor="system",
                    action="XCHAT_EVENT_RECEIVED",
                    subject_type="raw_event",
                    subject_id=str(raw_event_id),
                    detail={
                        "account_id": str(account.id),
                        "event_type": event_type,
                        "event_uuid": (payload.get("data") or {}).get("event_uuid"),
                    },
                )
            )
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
    elif is_xchat:
        logger.error(
            "XCHAT_EVENT_RECEIVED raw_event_id=%s account=%s event_type=%s event_uuid=%s",
            raw_event_id,
            account.id,
            event_type,
            (payload.get("data") or {}).get("event_uuid"),
        )
    return {"accepted": True}
