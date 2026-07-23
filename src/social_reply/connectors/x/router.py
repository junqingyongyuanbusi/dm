import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import insert

from social_reply.application.event_ingestion.direct_actors import process_direct_event
from social_reply.application.platform_accounts import (
    find_platform_account_by_external_id,
    find_platform_account_by_public_id,
    find_platform_app_by_public_id,
)
from social_reply.connectors.x.adapter import XWebhookAdapter
from social_reply.connectors.x.signature import crc_response, verify_x_signature
from social_reply.domain.messages.canonical import CanonicalEvent, canonical_event_to_dict
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.queue.dispatch import dispatch_actor
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


def _ingress_plan(
    payload: dict,
    event_type: str,
    events: list[CanonicalEvent],
    *,
    legacy_enabled: bool,
    xchat_enabled: bool,
) -> tuple[list[CanonicalEvent], str, bool]:
    is_xchat = event_type.startswith("chat.")
    if is_xchat and not xchat_enabled:
        return [], "IGNORED_XCHAT_DISABLED", False
    has_legacy_dm = bool(payload.get("direct_message_events") or payload.get("dm_events"))
    if has_legacy_dm and not legacy_enabled:
        events = [event for event in events if event.reply_target.get("kind") != "dm"]
        if not events:
            return [], "IGNORED_X_LEGACY_DISABLED", False
    if is_xchat:
        return events, "XCHAT_DECRYPTION_PENDING", True
    return events, "PENDING" if events else "IGNORED_AT_INGRESS", False


async def _webhook_secret(public_id: str) -> str:
    app = await find_platform_app_by_public_id(platform_family="x", public_id=public_id)
    if app is not None:
        return app.credential_bundle["consumer_secret"]
    account = await find_platform_account_by_public_id(platform="x", public_id=public_id)
    if account is not None:
        return account.webhook_secret_bundle["consumer_secret"]
    raise HTTPException(status_code=404, detail="x_webhook_not_found")


async def _event_account(public_id: str, payload: dict):
    app = await find_platform_app_by_public_id(platform_family="x", public_id=public_id)
    if app is None:
        return await find_platform_account_by_public_id(platform="x", public_id=public_id)
    data = payload.get("data") or {}
    external_account_id = str(
        payload.get("for_user_id") or (data.get("filter") or {}).get("user_id") or ""
    )
    if not external_account_id:
        return None
    return await find_platform_account_by_external_id(
        platform="x",
        external_account_id=external_account_id,
        platform_app_id=app.id,
    )


@router.get("/webhooks/x/{public_id}")
async def x_crc(public_id: str, crc_token: str = Query()) -> dict[str, str]:
    return {
        "response_token": crc_response(
            consumer_secret=await _webhook_secret(public_id),
            crc_token=crc_token,
        )
    }


@router.post("/webhooks/x/{public_id}")
async def x_webhook(public_id: str, request: Request) -> dict[str, bool]:
    body = await request.body()
    if not verify_x_signature(
        consumer_secret=await _webhook_secret(public_id),
        body=body,
        signature=request.headers.get("X-Twitter-Webhooks-Signature"),
    ):
        raise HTTPException(status_code=401, detail="invalid_x_signature")
    payload = json.loads(body)
    account = await _event_account(public_id, payload)
    if account is None:
        raise HTTPException(status_code=404, detail="x_event_account_not_found")
    settings = get_settings()
    event_type = str((payload.get("data") or {}).get("event_type") or "")
    is_xchat = event_type.startswith("chat.")
    adapter = XWebhookAdapter(
        account_id=str(account.id),
        external_account_id=account.external_account_id,
    )
    events, processing_status, dispatch_xchat = _ingress_plan(
        payload,
        event_type,
        adapter.normalize(payload),
        legacy_enabled=settings.x_legacy_dm_enabled,
        xchat_enabled=settings.xchat_enabled,
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
    elif dispatch_xchat:
        from social_reply.application.event_ingestion.xchat_actors import process_xchat_event
        from social_reply.application.event_ingestion.xchat_webhook import (
            process_xchat_raw_event,
        )

        await dispatch_actor(
            process_xchat_event,
            str(raw_event_id),
            str(account.id),
            inline=lambda: process_xchat_raw_event(raw_event_id, account.id),
        )
        logger.info(
            "XCHAT_EVENT_RECEIVED raw_event_id=%s account=%s event_type=%s event_uuid=%s",
            raw_event_id,
            account.id,
            event_type,
            (payload.get("data") or {}).get("event_uuid"),
        )
    return {"accepted": True}
