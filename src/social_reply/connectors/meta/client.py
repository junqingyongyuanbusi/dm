import httpx

from social_reply.connectors.errors import PermanentSendError, RetryableSendError

# Graph API 发送错误码。Meta 把业务错误统一放进 error.code + error.error_subcode,
# 常见永久错:10=超出 24h 消息窗口(需 message tag)、190=token 失效、
# 200/803/551=对象不可达/权限不足、100=无效收件人;613=限流,可退避重试。
# 与 X 不同,Meta 发送失败 HTTP 状态码通常是 400,故必须解析 body 才能区分「重试有用」与否。
_META_RETRYABLE_CODES = {613, 4, 17, 341}  # 各类速率/临时限制
_META_PERMANENT_CODES = {10, 100, 190, 200, 803, 551, 2018065}


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
        self._raise_for_send_errors(response)
        response.raise_for_status()
        result = response.json()
        message_id = result.get("message_id") or result.get("id")
        if not message_id:
            raise RuntimeError(f"meta_send_failed:{result}")
        return str(message_id)

    @staticmethod
    def _raise_for_send_errors(response: httpx.Response) -> None:
        """解析 Graph 错误 body:区分「超窗口/token 失效」等永久错与「限流」暂时错。

        不这样分类的话,超出 24h 窗口(code 10)会当成普通 4xx 无限退避重试,
        既刷爆日志又永远发不出;运营也看不到真正原因。
        """
        if response.status_code >= 500 or response.is_success:
            return
        try:
            error = response.json().get("error") or {}
        except ValueError:
            return
        code = error.get("code")
        subcode = error.get("error_subcode")
        detail = str(error.get("message", ""))[:200]
        if response.status_code == 429 or code in _META_RETRYABLE_CODES:
            raise RetryableSendError("META_RATE_LIMITED", detail)
        if code in _META_PERMANENT_CODES:
            label = "META_WINDOW_EXPIRED" if code == 10 else f"META_SEND_REJECTED_{code}"
            raise PermanentSendError(f"{label}:{subcode}" if subcode else label, detail)

    async def get_account(self) -> dict:
        response = await self._client.get(
            f"/{self._external_account_id}",
            params={"fields": "id,name"},
        )
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
