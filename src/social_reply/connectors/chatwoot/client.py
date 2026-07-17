from typing import Protocol

import httpx

from social_reply.shared.config import get_settings


class ChatwootClient(Protocol):
    async def create_message(
        self, *, account_id: int, conversation_id: int, content: str, private: bool
    ) -> int:
        """向 Chatwoot 会话发一条 outgoing 消息（private=True 为私有备注），
        返回 Chatwoot message id。"""
        ...


class FakeChatwootClient:
    """测试用：记录发送、返回自增 id。供集成测试内省 .sent。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._next_id = 1000

    async def create_message(
        self, *, account_id: int, conversation_id: int, content: str, private: bool
    ) -> int:
        self._next_id += 1
        self.sent.append(
            {
                "account_id": account_id,
                "conversation_id": conversation_id,
                "content": content,
                "private": private,
                "id": self._next_id,
            }
        )
        return self._next_id


class HttpxChatwootClient:
    """生产 Chatwoot Client；复用 AsyncClient 的连接池和 TLS 会话降低发送延迟。"""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"api_access_token": api_token},
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=2.0),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=60.0,
            ),
            transport=transport,
        )

    async def create_message(
        self, *, account_id: int, conversation_id: int, content: str, private: bool
    ) -> int:
        url = f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
        resp = await self._client.post(
            url,
            json={"content": content, "message_type": "outgoing", "private": private},
        )
        resp.raise_for_status()
        return int(resp.json()["id"])

    async def aclose(self) -> None:
        await self._client.aclose()


_fake: FakeChatwootClient | None = None
_http: HttpxChatwootClient | None = None


def get_chatwoot_client() -> ChatwootClient:
    settings = get_settings()
    if settings.testing:
        global _fake
        if _fake is None:
            _fake = FakeChatwootClient()
        return _fake
    global _http
    if _http is None:
        _http = HttpxChatwootClient(settings.chatwoot_base_url, settings.chatwoot_api_token)
    return _http
