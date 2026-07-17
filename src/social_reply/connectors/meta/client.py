import httpx


class MetaGraphClient:
    """Facebook Messenger / Instagram Messaging / Meta 评论统一发送客户端。"""

    def __init__(
        self,
        *,
        platform: str,
        access_token: str,
        external_account_id: str,
        graph_base_url: str = "https://graph.facebook.com",
        api_version: str = "v23.0",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if platform not in {"facebook", "instagram"}:
            raise ValueError(f"unsupported_meta_platform:{platform}")
        self.platform = platform
        self._external_account_id = external_account_id
        self._client = httpx.AsyncClient(
            base_url=f"{graph_base_url.rstrip('/')}/{api_version.strip('/')}",
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=2.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=transport,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def send_text(self, *, target: dict, text: str) -> str:
        kind = target.get("kind", "dm")
        if kind == "dm":
            response = await self._client.post(
                f"/{self._external_account_id}/messages",
                json={"recipient": {"id": target["recipient_id"]}, "message": {"text": text}},
            )
        elif kind == "comment":
            response = await self._client.post(
                f"/{target['comment_id']}/comments",
                json={"message": text},
            )
        elif kind == "private_reply":
            response = await self._client.post(
                f"/{self._external_account_id}/messages",
                json={"recipient": {"comment_id": target["comment_id"]}, "message": {"text": text}},
            )
        else:
            raise ValueError(f"unsupported_meta_target:{kind}")
        response.raise_for_status()
        result = response.json()
        message_id = result.get("message_id") or result.get("id")
        if not message_id:
            raise RuntimeError(f"meta_send_failed:{result}")
        return str(message_id)

    async def get_account(self) -> dict:
        response = await self._client.get(
            f"/{self._external_account_id}",
            params={"fields": "id,name"},
        )
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
