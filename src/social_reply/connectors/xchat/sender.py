import uuid

import httpx
from authlib.oauth1 import ClientAuth

from social_reply.connectors.errors import PermanentSendError, RetryableSendError
from social_reply.connectors.xchat.crypto import import_private_key_b64
from social_reply.connectors.xchat.key_cache import canonical_conversation_id


class XChatSender:
    platform = "x"

    def __init__(
        self,
        *,
        consumer_key: str,
        consumer_secret: str,
        access_token: str,
        access_token_secret: str,
        external_account_id: str,
        private_keys_b64: str,
        signing_key_version: str,
        conversation_key_events: dict[str, list[str]] | None = None,
        api_base_url: str = "https://api.x.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = api_base_url.rstrip("/")
        self._external_account_id = external_account_id
        self._private_keys_b64 = private_keys_b64
        self._signing_key_version = signing_key_version
        self._conversation_key_events = {
            canonical_conversation_id(key): list(value)
            for key, value in (conversation_key_events or {}).items()
        }
        self._auth = ClientAuth(
            consumer_key,
            consumer_secret,
            token=access_token,
            token_secret=access_token_secret,
            force_include_body=False,
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=3.0, read=15.0, write=15.0, pool=2.0),
            transport=transport,
        )

    async def send_text(self, *, target: dict, text: str) -> str:
        if target.get("kind") != "x_chat":
            raise ValueError(f"unsupported_xchat_target:{target.get('kind')}")
        conversation_id = canonical_conversation_id(str(target["conversation_id"]))
        events = list(self._conversation_key_events.get(conversation_id) or [])
        if not events:
            events = await self._load_persisted_key_events(conversation_id)
        if not events:
            events = await self._read_history(conversation_id)
            if events:
                self._conversation_key_events[conversation_id] = events
        chat = import_private_key_b64(self._private_keys_b64)
        # import_keys restores private material but not the public signing-key
        # version required when creating a signed outgoing event.
        chat.set_key_version(self._signing_key_version)
        extracted = chat.extract_conversation_keys(events)
        keys = dict(extracted.get("keys") or {})
        version = extracted.get("latest_version")
        if not version or version not in keys:
            raise PermanentSendError(
                "XCHAT_CONVERSATION_KEY_MISSING",
                "No decryptable XChat conversation key is available",
            )
        message_id = str(target.get("message_id") or uuid.uuid4())
        encrypted = chat.encrypt_message(
            message_id,
            self._external_account_id,
            conversation_id,
            keys[version],
            text,
            str(version),
            self._signing_key_version,
        )
        payload = {
            "message_id": message_id,
            "encoded_message_create_event": encrypted.encrypted_content,
            "encoded_message_event_signature": encrypted.encoded_event_signature,
        }
        if target.get("conversation_token"):
            payload["conversation_token"] = target["conversation_token"]
        path_id = conversation_id.replace(":", "-")
        response = await self._post_json(
            f"/2/chat/conversations/{path_id}/messages",
            payload,
        )
        if response.status_code == 429:
            raise RetryableSendError("XCHAT_RATE_LIMITED", "429 too many requests")
        if response.status_code in {401, 403}:
            raise PermanentSendError("XCHAT_FORBIDDEN", response.text[:300])
        if 400 <= response.status_code < 500:
            raise PermanentSendError(
                f"XCHAT_HTTP_{response.status_code}",
                response.text[:300],
            )
        response.raise_for_status()
        data = response.json().get("data") or response.json()
        return str(data.get("id") or data.get("message_id") or message_id)

    async def _load_persisted_key_events(self, conversation_id: str) -> list[str]:
        from sqlalchemy import select

        from social_reply.infrastructure.database import models
        from social_reply.infrastructure.database.engine import get_session_factory

        async with get_session_factory()() as session:
            config = await session.scalar(
                select(models.PlatformAccount.config).where(
                    models.PlatformAccount.external_account_id == self._external_account_id,
                    models.PlatformAccount.platform == "x",
                )
            )
        cached = {
            canonical_conversation_id(key): value
            for key, value in ((config or {}).get("xchat_conversation_key_events") or {}).items()
        }
        events = [str(item) for item in cached.get(conversation_id) or [] if item]
        if events:
            self._conversation_key_events[conversation_id] = events
        return events

    async def _read_history(self, conversation_id: str) -> list[str]:
        path_id = conversation_id.replace(":", "-")
        path = f"/2/chat/conversations/{path_id}/events"
        params = {
            "chat_message_event.fields": (
                "conversation_id,conversation_token,created_at,encoded_event,id,"
                "is_trusted,message_event_signature,previous_id,sender_id"
            )
        }
        query = "&".join(f"{key}={value}" for key, value in params.items())
        url = f"{self._base_url}{path}?{query}"
        _, headers, _ = self._auth.prepare("GET", url, {}, None)
        response = await self._client.get(path, params=params, headers=headers)
        if response.status_code == 404:
            fallback_path = f"/2/chat/conversations/{path_id}"
            fallback_url = f"{self._base_url}{fallback_path}?{query}"
            _, headers, _ = self._auth.prepare("GET", fallback_url, {}, None)
            response = await self._client.get(fallback_path, params=params, headers=headers)
        response.raise_for_status()
        payload = response.json()
        meta = payload.get("meta") or {}
        events = [
            str(value)
            for value in (
                meta.get("conversation_key_events")
                or meta.get("missing_conversation_key_change_events")
                or []
            )
            if value
        ]
        return events

    async def _post_json(self, path: str, payload: dict) -> httpx.Response:
        import json

        url = f"{self._base_url}{path}"
        body = json.dumps(payload, separators=(",", ":")).encode()
        _, headers, _ = self._auth.prepare(
            "POST",
            url,
            {"Content-Type": "application/json"},
            body,
        )
        return await self._client.post(path, content=body, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()


class DualXSender:
    platform = "x"

    def __init__(self, *, legacy, xchat: XChatSender) -> None:
        self._legacy = legacy
        self._xchat = xchat

    async def send_text(self, *, target: dict, text: str) -> str:
        if target.get("kind") == "x_chat":
            return await self._xchat.send_text(target=target, text=text)
        return await self._legacy.send_text(target=target, text=text)

    async def aclose(self) -> None:
        await self._legacy.aclose()
        await self._xchat.aclose()
