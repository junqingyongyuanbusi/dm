from dataclasses import dataclass
from typing import Any

import httpx


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
        api_base_url: str = "https://open.feishu.cn",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
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
                retryable=code == 99991400,
            )
        if response.status_code >= 400:
            raise FeishuClientError(
                f"FEISHU_HTTP_{response.status_code}",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        if not isinstance(payload, dict) or type(code) is not int:
            raise FeishuClientError("FEISHU_INVALID_RESPONSE", retryable=False)
        return payload

    async def tenant_access_token(self) -> tuple[str, int]:
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

    async def get_bot_info(self, tenant_access_token: str) -> FeishuBotInfo:
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
        if activate_status != 2:
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

    async def aclose(self) -> None:
        await self._client.aclose()
