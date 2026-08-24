import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from social_reply.connectors.errors import PermanentSendError, RetryableSendError
from social_reply.connectors.feishu.contracts import FEISHU_API_BASE_URL

_TOKEN_INVALID_CODE = 99991663
_RATE_LIMIT_CODES = frozenset({99991400})
_MAX_REPLY_BODY_BYTES = 20 * 1024
_MAX_CARD_BODY_BYTES = 100 * 1024
_MAX_PROVIDER_UUID_CHARS = 50


@dataclass(frozen=True)
class FeishuBotInfo:
    open_id: str
    name: str
    activate_status: int


class FeishuClientError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class FeishuClient:
    platform = "feishu"

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        api_base_url: str = FEISHU_API_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._clock = clock
        self._tenant_token: str | None = None
        self._tenant_token_generation = 0
        self._tenant_token_refresh_at = 0.0
        self._token_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=api_base_url.rstrip("/"),
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=2.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=transport,
        )

    @staticmethod
    def _payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        code = payload.get("code") if isinstance(payload, dict) else None
        if type(code) is int and code != 0:
            raise FeishuClientError(
                f"FEISHU_API_{code}",
                retryable=code in _RATE_LIMIT_CODES,
            )
        if response.status_code >= 400:
            raise FeishuClientError(
                f"FEISHU_HTTP_{response.status_code}",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        if not isinstance(payload, dict) or type(code) is not int:
            raise FeishuClientError("FEISHU_INVALID_RESPONSE", retryable=False)
        return payload

    async def _fetch_tenant_access_token(self) -> tuple[str, int]:
        response = await self._client.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        payload = self._payload(response)
        token = payload.get("tenant_access_token")
        expire = payload.get("expire")
        if not isinstance(token, str) or not token.strip():
            raise FeishuClientError("FEISHU_TOKEN_MISSING", retryable=False)
        if type(expire) is not int or not 60 <= expire <= 86400:
            raise FeishuClientError("FEISHU_TOKEN_EXPIRE_INVALID", retryable=False)
        return token.strip(), expire

    async def _tenant_access_token(
        self,
        *,
        rejected_generation: int | None = None,
    ) -> tuple[str, int, int]:
        now = self._clock()
        if (
            self._tenant_token
            and self._tenant_token_generation != rejected_generation
            and now < self._tenant_token_refresh_at
        ):
            return (
                self._tenant_token,
                max(1, int(self._tenant_token_refresh_at - now)),
                self._tenant_token_generation,
            )
        async with self._token_lock:
            now = self._clock()
            if (
                self._tenant_token
                and self._tenant_token_generation != rejected_generation
                and now < self._tenant_token_refresh_at
            ):
                return (
                    self._tenant_token,
                    max(1, int(self._tenant_token_refresh_at - now)),
                    self._tenant_token_generation,
                )
            token, expire = await self._fetch_tenant_access_token()
            refresh_margin = min(300, max(30, expire // 10))
            self._tenant_token = token
            self._tenant_token_generation += 1
            self._tenant_token_refresh_at = now + expire - refresh_margin
            return token, expire, self._tenant_token_generation

    async def tenant_access_token(self) -> tuple[str, int]:
        token, expire, _generation = await self._tenant_access_token()
        return token, expire

    async def get_bot_info(
        self,
        tenant_access_token: str,
        *,
        require_active: bool = True,
    ) -> FeishuBotInfo:
        response = await self._client.get(
            "/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {tenant_access_token}"},
        )
        payload = self._payload(response)
        bot = payload.get("bot")
        if not isinstance(bot, dict):
            raise FeishuClientError("FEISHU_BOT_MISSING", retryable=False)
        open_id = bot.get("open_id")
        if not isinstance(open_id, str) or not open_id.strip():
            raise FeishuClientError("FEISHU_BOT_OPEN_ID_MISSING", retryable=False)
        activate_status = bot.get("activate_status")
        if type(activate_status) is not int:
            raise FeishuClientError("FEISHU_BOT_STATUS_INVALID", retryable=False)
        if require_active and activate_status != 2:
            raise FeishuClientError("FEISHU_BOT_NOT_ACTIVATED", retryable=False)
        name = bot.get("app_name") or bot.get("name") or open_id
        if not isinstance(name, str) or not name.strip():
            name = open_id
        return FeishuBotInfo(
            open_id=open_id.strip(),
            name=name.strip(),
            activate_status=activate_status,
        )

    async def inspect_bot(self) -> FeishuBotInfo:
        token, _expire = await self.tenant_access_token()
        return await self.get_bot_info(token)

    @staticmethod
    def _reply_body(*, target: Mapping[str, Any], text: str) -> bytes:
        message_id = target.get("message_id")
        provider_uuid = target.get("uuid")
        if not isinstance(message_id, str) or not message_id.strip():
            raise PermanentSendError("FEISHU_API_TARGET_INVALID")
        if not isinstance(provider_uuid, str) or not provider_uuid.strip():
            raise PermanentSendError("FEISHU_API_UUID_INVALID")
        body: dict[str, Any] = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":")),
            "uuid": provider_uuid,
        }
        if target.get("thread_id") or target.get("root_id"):
            body["reply_in_thread"] = True
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_REPLY_BODY_BYTES:
            raise PermanentSendError("FEISHU_API_REQUEST_TOO_LARGE")
        return encoded

    async def _authenticated_request(
        self,
        request: Callable[[str], Awaitable[httpx.Response]],
    ) -> tuple[httpx.Response, dict[str, Any] | None, int | None]:
        try:
            token, _expire, token_generation = await self._tenant_access_token()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, FeishuClientError) else "FEISHU_TOKEN_UNAVAILABLE"
            raise RetryableSendError(code) from exc

        for refresh_attempt in range(2):
            response = await request(token)
            try:
                payload = response.json()
            except ValueError:
                payload = None
            code = payload.get("code") if isinstance(payload, dict) else None
            code = code if type(code) is int else None
            if code == _TOKEN_INVALID_CODE and refresh_attempt == 0:
                try:
                    token, _expire, token_generation = await self._tenant_access_token(
                        rejected_generation=token_generation
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    raise
                except Exception as exc:
                    error_code = (
                        exc.code
                        if isinstance(exc, FeishuClientError)
                        else "FEISHU_TOKEN_UNAVAILABLE"
                    )
                    raise RetryableSendError(error_code) from exc
                continue
            return response, payload, code
        raise PermanentSendError(f"FEISHU_API_{_TOKEN_INVALID_CODE}")

    @staticmethod
    def _response_payload(
        response: httpx.Response,
        payload: dict[str, Any] | None,
        code: int | None,
        *,
        operation: str,
    ) -> dict[str, Any]:
        if response.status_code == 429 or code in _RATE_LIMIT_CODES:
            raise RetryableSendError(f"FEISHU_API_{code or 429}")
        if response.status_code >= 500:
            raise httpx.HTTPStatusError(
                f"Feishu {operation} server error",
                request=response.request,
                response=response,
            )
        if response.status_code >= 400:
            raise PermanentSendError(f"FEISHU_API_{code or f'HTTP_{response.status_code}'}")
        if code is None or payload is None:
            raise FeishuClientError("FEISHU_INVALID_RESPONSE", retryable=False)
        if code != 0:
            raise PermanentSendError(f"FEISHU_API_{code}")
        return payload

    @staticmethod
    def _provider_message_id(payload: Mapping[str, Any]) -> str:
        data = payload.get("data")
        provider_message_id = data.get("message_id") if isinstance(data, dict) else None
        if not isinstance(provider_message_id, str) or not provider_message_id.strip():
            raise FeishuClientError("FEISHU_MESSAGE_ID_MISSING", retryable=False)
        return provider_message_id.strip()

    @staticmethod
    def _card_body(card: Mapping[str, Any], *, provider_uuid: str | None = None) -> bytes:
        content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        body: dict[str, Any] = {"content": content}
        if provider_uuid is not None:
            if not provider_uuid.strip() or len(provider_uuid) > _MAX_PROVIDER_UUID_CHARS:
                raise PermanentSendError("FEISHU_API_UUID_INVALID")
            body["uuid"] = provider_uuid
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_CARD_BODY_BYTES:
            raise PermanentSendError("FEISHU_API_REQUEST_TOO_LARGE")
        return encoded

    async def send_text(self, *, target: dict, text: str) -> str:
        body = self._reply_body(target=target, text=text)
        message_id = str(target["message_id"])
        response, payload, code = await self._authenticated_request(
            lambda token: self._client.post(
                f"/open-apis/im/v1/messages/{quote(message_id, safe='')}/reply",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                content=body,
            )
        )
        parsed = self._response_payload(response, payload, code, operation="reply")
        return self._provider_message_id(parsed)

    async def create_interactive_card(
        self,
        *,
        chat_id: str,
        card: Mapping[str, Any],
        provider_uuid: str,
    ) -> str:
        if not chat_id.strip():
            raise PermanentSendError("FEISHU_HANDOFF_CHAT_ID_INVALID")
        card_body = json.loads(self._card_body(card, provider_uuid=provider_uuid))
        body = json.dumps(
            {
                "receive_id": chat_id,
                "msg_type": "interactive",
                **card_body,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > _MAX_CARD_BODY_BYTES:
            raise PermanentSendError("FEISHU_API_REQUEST_TOO_LARGE")
        response, payload, code = await self._authenticated_request(
            lambda token: self._client.post(
                "/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                content=body,
            )
        )
        parsed = self._response_payload(response, payload, code, operation="card create")
        return self._provider_message_id(parsed)

    async def update_interactive_card(
        self,
        *,
        message_id: str,
        card: Mapping[str, Any],
    ) -> None:
        if not message_id.strip():
            raise PermanentSendError("FEISHU_MESSAGE_ID_MISSING")
        body = self._card_body(card)
        response, payload, code = await self._authenticated_request(
            lambda token: self._client.patch(
                f"/open-apis/im/v1/messages/{quote(message_id, safe='')}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                content=body,
            )
        )
        self._response_payload(response, payload, code, operation="card update")

    async def aclose(self) -> None:
        await self._client.aclose()
