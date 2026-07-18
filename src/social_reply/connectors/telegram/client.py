import re

import httpx


class TelegramClient:
    platform = "telegram"

    def __init__(
        self,
        *,
        token: str,
        api_base_url: str = "https://api.telegram.org",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=api_base_url.rstrip("/"),
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=2.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=transport,
        )

    def _path(self, method: str) -> str:
        return f"/bot{self._token}/{method}"

    def sanitize_error(self, value: str) -> str:
        return re.sub(r"/bot[^/]+/", "/bot<redacted>/", value).replace(self._token, "<redacted>")

    async def get_me(self) -> dict:
        response = await self._client.get(self._path("getMe"))
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError("telegram_get_me_failed")
        return result["result"]

    async def send_text(self, *, target: dict, text: str) -> str:
        response = await self._client.post(
            self._path("sendMessage"),
            json={"chat_id": target["chat_id"], "text": text},
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError("telegram_send_failed")
        return str(result["result"]["message_id"])

    async def set_webhook(
        self,
        *,
        url: str,
        secret_token: str,
        drop_pending_updates: bool = False,
    ) -> None:
        response = await self._client.post(
            self._path("setWebhook"),
            json={
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "edited_message"],
                "drop_pending_updates": drop_pending_updates,
            },
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError("telegram_set_webhook_failed")

    async def get_webhook_info(self) -> dict:
        response = await self._client.get(self._path("getWebhookInfo"))
        response.raise_for_status()
        return response.json()["result"]

    async def aclose(self) -> None:
        await self._client.aclose()
