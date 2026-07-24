import hashlib
import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import insert

from social_reply.application.event_ingestion.raw_recovery import (
    direct_dispatch_context,
    dispatch_initial_raw_event,
)
from social_reply.application.platform_accounts import (
    find_platform_account_by_external_id,
    find_platform_app_by_public_id,
)
from social_reply.connectors.meta.adapter import MetaWebhookAdapter
from social_reply.connectors.meta.signature import verify_meta_challenge, verify_meta_signature
from social_reply.connectors.whatsapp.adapter import WhatsAppWebhookAdapter
from social_reply.domain.messages.canonical import canonical_event_to_dict
from social_reply.domain.platform_accounts import (
    CapabilityKey,
    capability_enabled,
    normalize_account_capability,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


async def _find_meta_app(public_id: str):
    for family in ("meta", "instagram"):
        app = await find_platform_app_by_public_id(
            platform_family=family,
            public_id=public_id,
        )
        if app is not None:
            return app
    raise HTTPException(status_code=404, detail="meta_app_not_found")


@router.get("/webhooks/meta/{app_public_id}")
async def verify_meta_webhook(
    app_public_id: str,
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> Response:
    app = await _find_meta_app(app_public_id)
    challenge = verify_meta_challenge(
        verify_token=app.credential_bundle["verify_token"],
        mode=hub_mode,
        token=hub_verify_token,
        challenge=hub_challenge,
    )
    if challenge is None:
        raise HTTPException(status_code=403, detail="invalid_meta_challenge")
    return Response(content=challenge, media_type="text/plain")


async def _record_disabled_request(
    *,
    app,
    app_public_id: str,
    object_type: str,
    body: bytes,
    disabled_code: str,
) -> None:
    payload_digest = hashlib.sha256(body).hexdigest()
    async with get_session_factory()() as session:
        await session.execute(
            insert(models.RawEvent).values(
                tenant_id=app.tenant_id,
                source="meta",
                ingress_kind="webhook_gate",
                event_namespace="meta.disabled",
                external_event_id=payload_digest,
                payload={"object": object_type},
                headers={
                    "signature_verified": True,
                    "ingress_gate": disabled_code,
                    "payload_sha256": payload_digest,
                },
                context={
                    "app_id": str(app.id),
                    "app_public_id": app_public_id,
                },
                processing_status="IGNORED_AT_INGRESS",
                processed_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def _handle_meta_messaging_request(
    *,
    app,
    app_public_id: str,
    object_type: str,
    platform: str,
    payload: dict,
    body: bytes,
) -> Response:
    payload_digest = hashlib.sha256(body).hexdigest()
    entries = payload.get("entry")
    if not isinstance(entries, list):
        entries = []
    occurrences: list[tuple[object, dict, list[dict]]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        external_account_id = str(entry.get("id", ""))
        if not external_account_id:
            continue
        account = await find_platform_account_by_external_id(
            platform=platform,
            external_account_id=external_account_id,
            platform_app_id=app.id,
        )
        if account is None or account.tenant_id != app.tenant_id:
            logger.warning(
                "meta event ignored: unregistered account app=%s platform=%s account=%s",
                app.id,
                platform,
                external_account_id,
            )
            continue
        try:
            capability = normalize_account_capability(platform, account.capability)
        except ValueError:
            logger.error("meta event ignored: invalid capability account=%s", account.id)
            allow_dm = False
            allow_comments = False
        else:
            allow_dm = capability_enabled(capability, CapabilityKey.DM)
            allow_comments = capability_enabled(capability, CapabilityKey.COMMENTS)
        adapter = MetaWebhookAdapter(
            platform=platform,
            account_id=str(account.id),
            external_account_id=external_account_id,
            allow_dm=allow_dm,
            allow_comments=allow_comments,
        )
        events = adapter.normalize({"object": object_type, "entry": [entry]})
        occurrences.append(
            (
                account,
                entry,
                [canonical_event_to_dict(event) for event in events],
            )
        )

    dispatch_ids = []
    async with get_session_factory()() as session:
        request_raw_event_id = (
            await session.execute(
                insert(models.RawEvent)
                .values(
                    tenant_id=app.tenant_id,
                    source="meta",
                    ingress_kind="webhook_request",
                    event_namespace=f"meta.{platform}.request",
                    external_event_id=payload_digest,
                    payload={
                        "object": object_type,
                        "entry_count": len(entries),
                    },
                    headers={
                        "signature_verified": True,
                        "payload_sha256": payload_digest,
                    },
                    context={
                        "app_id": str(app.id),
                        "app_public_id": app_public_id,
                    },
                    processing_status="VERIFIED_REQUEST",
                    processed_at=datetime.now(UTC),
                )
                .returning(models.RawEvent.id)
            )
        ).scalar_one()
        for account, entry, serialized_events in occurrences:
            raw_event_id = (
                await session.execute(
                    insert(models.RawEvent)
                    .values(
                        tenant_id=account.tenant_id,
                        platform_account_id=account.id,
                        source="meta",
                        ingress_kind="webhook",
                        event_namespace=f"meta.{platform}.entry",
                        payload={"object": object_type, "entry": [entry]},
                        headers={
                            "signature_verified": True,
                            "payload_sha256": payload_digest,
                        },
                        context={
                            "app_id": str(app.id),
                            "app_public_id": app_public_id,
                            "request_raw_event_id": str(request_raw_event_id),
                            **(
                                direct_dispatch_context(serialized_events)
                                if serialized_events
                                else {}
                            ),
                        },
                        processing_status=(
                            "PENDING" if serialized_events else "IGNORED_AT_INGRESS"
                        ),
                        processed_at=None if serialized_events else datetime.now(UTC),
                    )
                    .returning(models.RawEvent.id)
                )
            ).scalar_one()
            if serialized_events:
                dispatch_ids.append(raw_event_id)
        await session.commit()
    for raw_event_id in dispatch_ids:
        await dispatch_initial_raw_event(raw_event_id)
    return Response(status_code=200)


@router.post("/webhooks/meta/{app_public_id}")
async def meta_webhook(app_public_id: str, request: Request) -> Response:
    app = await _find_meta_app(app_public_id)
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
    platform = {
        "page": "facebook",
        "instagram": "instagram",
        "whatsapp_business_account": "whatsapp",
    }[object_type]
    disabled_code = get_settings().platform_disabled_code(platform)
    if disabled_code is not None:
        await _record_disabled_request(
            app=app,
            app_public_id=app_public_id,
            object_type=object_type,
            body=body,
            disabled_code=disabled_code,
        )
        return Response(status_code=200)
    if object_type in {"page", "instagram"}:
        return await _handle_meta_messaging_request(
            app=app,
            app_public_id=app_public_id,
            object_type=object_type,
            platform=platform,
            payload=payload,
            body=body,
        )

    queued_events = []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
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
                account_id=str(account.id),
                phone_number_id=phone_number_id,
            )
            queued_events.extend(
                adapter.normalize({"object": object_type, "entry": [{"changes": [change]}]})
            )
    serialized_events = [canonical_event_to_dict(event) for event in queued_events]
    async with get_session_factory()() as session:
        raw_event_id = (
            await session.execute(
                insert(models.RawEvent)
                .values(
                    tenant_id=app.tenant_id,
                    source="meta",
                    ingress_kind="webhook",
                    payload=payload,
                    headers={"signature_verified": True},
                    context=(
                        direct_dispatch_context(serialized_events) if serialized_events else {}
                    ),
                    processing_status=("PENDING" if serialized_events else "IGNORED_AT_INGRESS"),
                )
                .returning(models.RawEvent.id)
            )
        ).scalar_one()
        await session.commit()
    if serialized_events:
        await dispatch_initial_raw_event(raw_event_id)
    return Response(status_code=200)
