import json

import httpx
from authlib.oauth1 import ClientAuth

from social_reply.connectors.errors import PermanentSendError, RetryableSendError

# X v2 发送侧业务错误码 → 领域异常。349「对方不收 DM」(未关注/拉黑/关闭陌生人私信)
# 重试无意义;X 把它包在 200 或 4xx 里,单看 HTTP 状态码判不出,必须解析 body。
_X_PERMANENT_CODES = {349, 150, 151}


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
        _, headers, _ = self._auth.prepare("POST", url, {"Content-Type": "application/json"}, body)
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
        self._raise_for_send_errors(response)
        response.raise_for_status()
        result = response.json()
        data = result.get("data") or result
        message_id = data.get("dm_event_id") or data.get("id")
        if not message_id:
            raise RuntimeError(f"x_send_failed:{result}")
        return str(message_id)

    @staticmethod
    def _raise_for_send_errors(response: httpx.Response) -> None:
        """解析 X 业务错误:非 5xx 响应的 body 里可能带 errors 数组(即使 HTTP 200)。"""
        if response.status_code >= 500:
            return
        if response.status_code == 429:
            raise RetryableSendError("X_RATE_LIMITED", "429 too many requests")
        try:
            errors = response.json().get("errors") or []
        except ValueError:
            return
        for error in errors:
            code = error.get("code")
            if code in _X_PERMANENT_CODES:
                raise PermanentSendError(f"X_CANNOT_DM_{code}", str(error.get("message", ""))[:200])
        if response.status_code == 403 and errors:
            # 403 + 业务错误 = 权限/对象侧拒绝(scope 不足、对方设置),重试无意义
            raise PermanentSendError("X_FORBIDDEN", str(errors[0].get("message", ""))[:200])

    async def get_me(self) -> dict:
        """GET 无 body，OAuth1 签名头预生成即可。"""
        url = f"{self._base_url}/2/users/me?user.fields=id,name,username"
        _, headers, _ = self._auth.prepare("GET", url, {}, None)
        response = await self._client.get(
            "/2/users/me", params={"user.fields": "id,name,username"}, headers=headers
        )
        response.raise_for_status()
        return response.json()["data"]

    async def read_dm_events(
        self, *, max_results: int = 50, pagination_token: str | None = None
    ) -> tuple[list[dict], str | None]:
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
        if pagination_token:
            params["pagination_token"] = pagination_token
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self._base_url}/2/dm_events?{query}"
        _, headers, _ = self._auth.prepare("GET", url, {}, None)
        response = await self._client.get("/2/dm_events", params=params, headers=headers)
        response.raise_for_status()
        payload = response.json()
        return payload.get("data", []), (payload.get("meta") or {}).get("next_token")

    async def read_conversation_dm_events(
        self, participant_id: str, *, max_results: int = 50
    ) -> list[dict]:
        """按会话拉取与指定用户的 DM 事件。

        与全局 /2/dm_events 各自独立限流(实测各 15 req/15min),且实测中
        全局端点有官方未修复的漏消息 bug,本端点可作对照/补拉来源(诊断脚本用)。
        """
        params = {
            "max_results": str(max_results),
            "event_types": "MessageCreate",
            "dm_event.fields": "id,text,event_type,created_at,sender_id,dm_conversation_id",
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        path = f"/2/dm_conversations/with/{participant_id}/dm_events"
        url = f"{self._base_url}{path}?{query}"
        _, headers, _ = self._auth.prepare("GET", url, {}, None)
        response = await self._client.get(path, params=params, headers=headers)
        response.raise_for_status()
        return response.json().get("data", [])

    async def aclose(self) -> None:
        await self._client.aclose()
