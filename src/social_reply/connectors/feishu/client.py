import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from social_reply.connectors.errors import PermanentSendError, RetryableSendError
from social_reply.connectors.feishu.contracts import FEISHU_API_BASE_URL

_TOKEN_INVALID_CODE = 99991663
_RATE_LIMIT_CODES = frozenset({99991400})
_MAX_REPLY_BODY_BYTES = 20 * 1024


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

    async def _send_reply(
        self,
        *,
        target: Mapping[str, Any],
        body: bytes,
        token: str,
    ) -> tuple[httpx.Response, dict[str, Any] | None, int | None]:
        message_id = str(target["message_id"])
        response = await self._client.post(
            f"/open-apis/im/v1/messages/{quote(message_id, safe='')}/reply",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            content=body,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        code = payload.get("code") if isinstance(payload, dict) else None
        return response, payload, code if type(code) is int else None

    async def send_text(self, *, target: dict, text: str) -> str:
        body = self._reply_body(target=target, text=text)
        try:
            token, _expire, token_generation = await self._tenant_access_token()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, FeishuClientError) else "FEISHU_TOKEN_UNAVAILABLE"
            raise RetryableSendError(code) from exc

        for refresh_attempt in range(2):
            response, payload, code = await self._send_reply(target=target, body=body, token=token)
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
            if response.status_code == 429 or code in _RATE_LIMIT_CODES:
                raise RetryableSendError(f"FEISHU_API_{code or 429}")
            if response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    "Feishu reply server error",
                    request=response.request,
                    response=response,
                )
            if response.status_code >= 400:
                raise PermanentSendError(f"FEISHU_API_{code or f'HTTP_{response.status_code}'}")
            if code is None or payload is None:
                raise FeishuClientError("FEISHU_INVALID_RESPONSE", retryable=False)
            if code != 0:
                raise PermanentSendError(f"FEISHU_API_{code}")
            data = payload.get("data")
            provider_message_id = data.get("message_id") if isinstance(data, dict) else None
            if not isinstance(provider_message_id, str) or not provider_message_id.strip():
                raise FeishuClientError("FEISHU_MESSAGE_ID_MISSING", retryable=False)
            return provider_message_id.strip()
        raise PermanentSendError(f"FEISHU_API_{_TOKEN_INVALID_CODE}")

    async def aclose(self) -> None:
        await self._client.aclose()
