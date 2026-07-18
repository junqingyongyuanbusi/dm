import json

import httpx
from authlib.oauth1 import ClientAuth


class XClient:
    """X v2 发送客户端；OAuth 1.0a 签名头预生成后随请求发送。

    注意：不能用 authlib 的 OAuth1Auth（httpx auth flow）——它在为 POST 签名时会
    重建 request 并清空 JSON body（body 变成 b''），导致 X 返回 400 "text required"。
    改用底层 ClientAuth.prepare() 预生成 Authorization 头，body 用 content= 原样发送。
    JSON body 不参与 OAuth1 签名（force_include_body=False，符合规范）。
    """

    platform = "x"

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
        self._base_url = api_base_url.rstrip("/")
        self._auth = ClientAuth(
            consumer_key,
            consumer_secret,
            token=access_token,
            token_secret=access_token_secret,
            force_include_body=False,
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=2.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=transport,
        )

    async def _post_json(self, path: str, payload: dict) -> httpx.Response:
        """带 OAuth1 签名头的 JSON POST；签名头预生成，body 原样发送。"""
        url = f"{self._base_url}{path}"
        body = json.dumps(payload).encode()
        _, headers, _ = self._auth.prepare(
            "POST", url, {"Content-Type": "application/json"}, body
        )
        return await self._client.post(path, content=body, headers=headers)

    async def send_text(self, *, target: dict, text: str) -> str:
        kind = target.get("kind", "dm")
        if kind == "dm":
            response = await self._post_json(
                f"/2/dm_conversations/with/{target['participant_id']}/messages",
                {"text": text},
            )
        elif kind == "reply":
            response = await self._post_json(
                "/2/tweets",
                {"text": text, "reply": {"in_reply_to_tweet_id": target["in_reply_to_post_id"]}},
            )
        else:
            raise ValueError(f"unsupported_x_target:{kind}")
        response.raise_for_status()
        result = response.json()
        data = result.get("data") or result
        message_id = data.get("dm_event_id") or data.get("id")
        if not message_id:
            raise RuntimeError(f"x_send_failed:{result}")
        return str(message_id)

    async def get_me(self) -> dict:
        """GET 无 body，OAuth1 签名头预生成即可。"""
        url = f"{self._base_url}/2/users/me?user.fields=id,name,username"
        _, headers, _ = self._auth.prepare("GET", url, {}, None)
        response = await self._client.get(
            "/2/users/me", params={"user.fields": "id,name,username"}, headers=headers
        )
        response.raise_for_status()
        return response.json()["data"]

    async def read_dm_events(self, *, max_results: int = 50) -> list[dict]:
        """拉取最近的 DM 事件（轮询用，替代不可靠的 webhook 推送）。

        返回按时间倒序的 MessageCreate 事件列表，每条含 id/text/sender_id/
        dm_conversation_id/created_at。X 的 Account Activity webhook 投递不稳定，
        改由 scheduler 定时调本方法主动拉取。
        """
        params = {
            "max_results": str(max_results),
            "event_types": "MessageCreate",
            "dm_event.fields": "id,text,event_type,created_at,sender_id,dm_conversation_id",
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self._base_url}/2/dm_events?{query}"
        _, headers, _ = self._auth.prepare("GET", url, {}, None)
        response = await self._client.get("/2/dm_events", params=params, headers=headers)
        response.raise_for_status()
        return response.json().get("data", [])

    async def aclose(self) -> None:
        await self._client.aclose()
