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
        self.sent.append({
            "account_id": account_id, "conversation_id": conversation_id,
            "content": content, "private": private, "id": self._next_id})
        return self._next_id


class HttpxChatwootClient:
    """生产：POST /api/v1/accounts/{account_id}/conversations/{conversation_id}/messages
    Header api_access_token；message_type=outgoing，private 决定是否私有备注。"""

    def __init__(
        self, base_url: str, api_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._transport = transport

    async def create_message(
        self, *, account_id: int, conversation_id: int, content: str, private: bool
    ) -> int:
        url = (f"{self._base_url}/api/v1/accounts/{account_id}"
               f"/conversations/{conversation_id}/messages")
        async with httpx.AsyncClient(timeout=15.0, transport=self._transport) as client:
            resp = await client.post(
                url,
                headers={"api_access_token": self._api_token},
                json={"content": content, "message_type": "outgoing", "private": private},
            )
            resp.raise_for_status()
            return int(resp.json()["id"])


_fake: FakeChatwootClient | None = None


def get_chatwoot_client() -> ChatwootClient:
    settings = get_settings()
    if settings.testing:
        global _fake
        if _fake is None:
            _fake = FakeChatwootClient()
        return _fake
    return HttpxChatwootClient(settings.chatwoot_base_url, settings.chatwoot_api_token)
