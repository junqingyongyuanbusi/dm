import httpx


class WhatsAppClient:
    platform = "whatsapp"

    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        graph_base_url: str = "https://graph.facebook.com",
        api_version: str = "v23.0",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._phone_number_id = phone_number_id
        self._client = httpx.AsyncClient(
            base_url=f"{graph_base_url.rstrip('/')}/{api_version.strip('/')}",
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=2.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=transport,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def get_phone_number(self) -> dict:
        response = await self._client.get(
            f"/{self._phone_number_id}",
            params={"fields": "id,display_phone_number,verified_name,quality_rating"},
        )
        response.raise_for_status()
        return response.json()

    async def send_text(self, *, target: dict, text: str) -> str:
        if target.get("kind", "session_message") != "session_message":
            raise ValueError(f"unsupported_whatsapp_target:{target.get('kind')}")
        response = await self._client.post(
            f"/{self._phone_number_id}/messages",
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": target["to"],
                "type": "text",
                "text": {"preview_url": False, "body": text},
            },
        )
        response.raise_for_status()
        result = response.json()
        messages = result.get("messages") or []
        if not messages or not messages[0].get("id"):
            raise RuntimeError(f"whatsapp_send_failed:{result}")
        return str(messages[0]["id"])

    async def aclose(self) -> None:
        await self._client.aclose()
