import base64
import json
from collections.abc import Mapping

import httpx
from authlib.integrations.httpx_client import AsyncOAuth1Client


class XChatClient:
    """Small async client for the X Chat and X Activity endpoints.

    X currently accepts the existing OAuth 1.0a user token for Chat reads and for
    creating a private ``chat.received`` subscription. App-only bearer auth is
    used for listing/deleting subscriptions and webhook management.
    """

    def __init__(
        self,
        *,
        consumer_key: str,
        consumer_secret: str,
        access_token: str,
        access_token_secret: str,
        api_base_url: str = "https://api.x.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._api_base_url = api_base_url.rstrip("/")
        self._user = AsyncOAuth1Client(
            consumer_key,
            consumer_secret,
            token=access_token,
            token_secret=access_token_secret,
            timeout=20,
            transport=transport,
        )
        self._app = httpx.AsyncClient(
            base_url=self._api_base_url,
            timeout=20,
            transport=transport,
        )
        self._app_bearer: str | None = None

    async def aclose(self) -> None:
        await self._user.aclose()
        await self._app.aclose()

    async def read_conversations(
        self,
        *,
        max_results: int = 100,
        pagination_token: str | None = None,
    ) -> tuple[list[dict], str | None]:
        params: dict[str, object] = {
            "max_results": max_results,
            "chat_conversation.fields": "id,participant_ids,type,updated_at",
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        response = await self._user.get(
            f"{self._api_base_url}/2/chat/conversations",
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
        return list(payload.get("data") or []), (payload.get("meta") or {}).get("next_token")

    async def read_conversation_events(
        self,
        conversation_id: str,
        *,
        pagination_token: str | None = None,
    ) -> tuple[list[dict], list[str], str | None]:
        params: dict[str, object] = {}
        if pagination_token:
            params["pagination_token"] = pagination_token
        params["chat_message_event.fields"] = (
            "conversation_id,conversation_token,created_at,encoded_event,id,"
            "is_trusted,message_event_signature,previous_id,sender_id"
        )
        path_id = conversation_id.replace(":", "-")
        # Conversation listings use colon-delimited canonical IDs, while the Chat
        # history path requires the same participant IDs separated by a hyphen.
        # The live X API currently exposes the dedicated /events route. xdk 0.9.0
        # was generated from an older spec where the same operation used the base
        # conversation path, so keep a compatibility fallback for either contract.
        response = await self._user.get(
            f"{self._api_base_url}/2/chat/conversations/{path_id}/events",
            params=params,
        )
        if response.status_code == 404:
            response = await self._user.get(
                f"{self._api_base_url}/2/chat/conversations/{path_id}",
                params=params,
            )
        response.raise_for_status()
        payload = response.json()
        meta = payload.get("meta") or {}
        return (
            list(payload.get("data") or []),
            list(
                meta.get("conversation_key_events")
                or meta.get("missing_conversation_key_change_events")
                or []
            ),
            meta.get("next_token"),
        )

    async def get_user_public_keys(self, user_id: str) -> list[dict]:
        response = await self._user.get(
            f"{self._api_base_url}/2/users/{user_id}/public_keys",
            params={
                "public_key.fields": (
                    "public_key_version,public_key,signing_public_key,"
                    "identity_public_key_signature,juicebox_config"
                )
            },
        )
        response.raise_for_status()
        return list(response.json().get("data") or [])

    async def create_activity_subscription(
        self,
        *,
        event_type: str,
        user_id: str,
        webhook_id: str | None = None,
        tag: str,
    ) -> dict:
        if event_type not in {"chat.received", "dm.received"}:
            raise ValueError(f"unsupported_x_activity_event_type:{event_type}")
        body: dict[str, object] = {
            "event_type": event_type,
            "filter": {"user_id": user_id},
            "tag": tag,
        }
        if webhook_id:
            body["webhook_id"] = webhook_id
        response = await self._oauth1_json_request(
            "POST",
            "/2/activity/subscriptions",
            body,
        )
        response.raise_for_status()
        return response.json()

    async def create_received_subscription(
        self,
        *,
        user_id: str,
        webhook_id: str | None = None,
        tag: str = "reply-core xchat",
    ) -> dict:
        return await self.create_activity_subscription(
            event_type="chat.received",
            user_id=user_id,
            webhook_id=webhook_id,
            tag=tag,
        )

    async def list_webhooks(self) -> list[dict]:
        bearer = await self._app_bearer_token()
        response = await self._app.get(
            "/2/webhooks",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        response.raise_for_status()
        return list(response.json().get("data") or [])

    async def list_subscriptions(self) -> list[dict]:
        bearer = await self._app_bearer_token()
        response = await self._app.get(
            "/2/activity/subscriptions",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        response.raise_for_status()
        return list(response.json().get("data") or [])

    async def _oauth1_json_request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object],
    ) -> httpx.Response:
        """Sign the request with OAuth1 without letting authlib strip the JSON body.

        ``AsyncOAuth1Client`` rebuilds JSON requests as form requests while applying
        OAuth1 auth. X does not include a JSON body in the OAuth1 signature, so sign
        an empty request and send the exact JSON bytes separately.
        """

        url = f"{self._api_base_url}{path}"
        request = self._user.build_request(method, url)
        async for signed in self._user.auth.async_auth_flow(request):
            authorization = signed.headers["Authorization"]
            break
        else:  # pragma: no cover - authlib always yields one signed request
            raise RuntimeError("x_oauth1_signing_failed")
        return await self._app.request(
            method,
            path,
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
            },
            content=json.dumps(dict(body), separators=(",", ":")).encode(),
        )

    async def _app_bearer_token(self) -> str:
        if self._app_bearer:
            return self._app_bearer
        basic = base64.b64encode(f"{self._consumer_key}:{self._consumer_secret}".encode()).decode()
        response = await self._app.post(
            "/oauth2/token",
            headers={"Authorization": f"Basic {basic}"},
            data={"grant_type": "client_credentials"},
        )
        response.raise_for_status()
        self._app_bearer = str(response.json()["access_token"])
        return self._app_bearer
