import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import insert

from social_reply.application.event_ingestion.direct_actors import process_direct_event
from social_reply.application.platform_accounts import (
    find_platform_account_by_external_id,
    find_platform_app_by_public_id,
)
from social_reply.connectors.meta.adapter import MetaWebhookAdapter
from social_reply.connectors.meta.signature import verify_meta_challenge, verify_meta_signature
from social_reply.connectors.whatsapp.adapter import WhatsAppWebhookAdapter
from social_reply.domain.messages.canonical import canonical_event_to_dict
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/webhooks/meta/{app_public_id}")
async def verify_meta_webhook(
    app_public_id: str,
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> Response:
    app = await find_platform_app_by_public_id(platform_family="meta", public_id=app_public_id)
    if app is None:
        raise HTTPException(status_code=404, detail="meta_app_not_found")
    challenge = verify_meta_challenge(
        verify_token=app.credential_bundle["verify_token"],
        mode=hub_mode,
        token=hub_verify_token,
        challenge=hub_challenge,
    )
    if challenge is None:
        raise HTTPException(status_code=403, detail="invalid_meta_challenge")
    return Response(content=challenge, media_type="text/plain")


@router.post("/webhooks/meta/{app_public_id}")
async def meta_webhook(app_public_id: str, request: Request) -> Response:
    app = await find_platform_app_by_public_id(platform_family="meta", public_id=app_public_id)
    if app is None:
        raise HTTPException(status_code=404, detail="meta_app_not_found")
    body = await request.body()
    if not verify_meta_signature(
        app_secret=app.credential_bundle["app_secret"],
        body=body,
        signature=request.headers.get("X-Hub-Signature-256"),
    ):
        raise HTTPException(status_code=401, detail="invalid_meta_signature")
    payload = json.loads(body)
    object_type = payload.get("object")
    if object_type not in {"page", "instagram", "whatsapp_business_account"}:
        return Response(status_code=200)
    queued_events = []
    for entry in payload.get("entry", []):
        if object_type == "whatsapp_business_account":
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                phone_number_id = str((value.get("metadata") or {}).get("phone_number_id", ""))
                if not phone_number_id:
                    continue
                account = await find_platform_account_by_external_id(
                    platform="whatsapp",
                    external_account_id=phone_number_id,
                    platform_app_id=app.id,
                )
                if account is None:
                    logger.warning(
                        "whatsapp event ignored: unregistered account app=%s phone=%s",
                        app.id,
                        phone_number_id,
                    )
                    continue
                adapter = WhatsAppWebhookAdapter(
                    account_id=str(account.id), phone_number_id=phone_number_id
                )
                queued_events.extend(
                    adapter.normalize({"object": object_type, "entry": [{"changes": [change]}]})
                )
            continue

        platform = "instagram" if object_type == "instagram" else "facebook"
        external_account_id = str(entry.get("id", ""))
        if not external_account_id:
            continue
        account = await find_platform_account_by_external_id(
            platform=platform,
            external_account_id=external_account_id,
            platform_app_id=app.id,
        )
        if account is None:
            logger.warning(
                "meta event ignored: unregistered account app=%s platform=%s account=%s",
                app.id,
                platform,
                external_account_id,
            )
            continue
        adapter = MetaWebhookAdapter(
            platform=platform,
            account_id=str(account.id),
            external_account_id=external_account_id,
        )
        queued_events.extend(adapter.normalize({"object": object_type, "entry": [entry]}))
    async with get_session_factory()() as session:
        raw_event_id = (
            await session.execute(
                insert(models.RawEvent)
                .values(
                    source="meta",
                    payload=payload,
                    headers={"signature_verified": True},
                    processing_status="PENDING" if queued_events else "IGNORED_AT_INGRESS",
                )
                .returning(models.RawEvent.id)
            )
        ).scalar_one()
        await session.commit()
    serialized_events = [canonical_event_to_dict(event) for event in queued_events]
    if serialized_events:
        from social_reply.application.event_ingestion.direct_actors import _process_events

        await dispatch_actor(
            process_direct_event,
            str(raw_event_id),
            serialized_events,
            inline=lambda: _process_events(raw_event_id, serialized_events),
        )
    return Response(status_code=200)
