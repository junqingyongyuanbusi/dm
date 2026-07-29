import hashlib
import hmac

import httpx

from social_reply.connectors.errors import PermanentSendError, RetryableSendError

# Graph API 发送错误码。Meta 把业务错误统一放进 error.code + error.error_subcode,
# 常见永久错:10=超出 24h 消息窗口(需 message tag)、190=token 失效、
# 200/803/551=对象不可达/权限不足、100=无效收件人;613=限流,可退避重试。
# 与 X 不同,Meta 发送失败 HTTP 状态码通常是 400,故必须解析 body 才能区分「重试有用」与否。
_META_RETRYABLE_CODES = {613, 4, 17, 341}  # 各类速率/临时限制
_META_PERMANENT_CODES = {10, 100, 190, 200, 803, 551, 2018065}


def appsecret_proof(access_token: str, app_secret: str) -> str:
    return hmac.new(
        app_secret.encode(),
        access_token.encode(),
        hashlib.sha256,
    ).hexdigest()


class MetaGraphClient:
    """Facebook Messenger / Instagram Messaging / Meta 评论统一发送客户端。"""

    def __init__(
        self,
        *,
        platform: str,
        access_token: str,
        app_secret: str,
        external_account_id: str,
        graph_base_url: str = "https://graph.facebook.com",
        api_version: str = "v23.0",
        instagram_login_mode: str = "facebook_login",
        page_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if platform not in {"facebook", "instagram"}:
            raise ValueError(f"unsupported_meta_platform:{platform}")
        if instagram_login_mode not in {"facebook_login", "instagram_login"}:
            raise ValueError(f"unsupported_instagram_login_mode:{instagram_login_mode}")
        if not app_secret.strip():
            raise ValueError("missing_meta_app_secret")
        self.platform = platform
        self._external_account_id = external_account_id
        self._instagram_login_mode = instagram_login_mode
        self._page_id = page_id
        self._client = httpx.AsyncClient(
            base_url=f"{graph_base_url.rstrip('/')}/{api_version.strip('/')}",
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=2.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=transport,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"appsecret_proof": appsecret_proof(access_token, app_secret)},
        )

    def _messaging_sender_id(self) -> str:
        if (
            self.platform == "instagram"
            and self._instagram_login_mode == "facebook_login"
            and self._page_id
        ):
            return self._page_id
        return self._external_account_id

    async def send_text(self, *, target: dict, text: str) -> str:
        kind = target.get("kind", "dm")
        if kind == "dm":
            response = await self._client.post(
                f"/{self._messaging_sender_id()}/messages",
                json={"recipient": {"id": target["recipient_id"]}, "message": {"text": text}},
            )
        elif kind == "comment":
            # Instagram 回复评论走 /replies，Facebook 走 /comments。两边同用 /comments 时
            # IG 侧会直接失败，而这条路径此前从未启用过，所以一直没暴露。
            edge = "replies" if self.platform == "instagram" else "comments"
            response = await self._client.post(
                f"/{target['comment_id']}/{edge}",
                json={"message": text},
            )
        elif kind == "private_reply":
            response = await self._client.post(
                f"/{self._messaging_sender_id()}/messages",
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
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else {}
        if not isinstance(error, dict):
            error = {}
        code = error.get("code")
        subcode = error.get("error_subcode")
        detail = str(error.get("message", ""))[:200]
        if (
            response.status_code == 429
            or error.get("is_transient") is True
            or code in _META_RETRYABLE_CODES
        ):
            label = f"META_RETRYABLE_{code}" if code is not None else "META_RATE_LIMITED"
            raise RetryableSendError(label, detail)
        if 400 <= response.status_code < 500:
            label = (
                "META_WINDOW_EXPIRED"
                if code == 10
                else f"META_SEND_REJECTED_{code or response.status_code}"
            )
            if code in _META_PERMANENT_CODES or code is not None:
                raise PermanentSendError(
                    f"{label}:{subcode}" if subcode else label,
                    detail,
                )
            raise PermanentSendError(label, detail or "Meta Graph API rejected the request")

    async def get_account(self) -> dict:
        if self.platform == "instagram" and self._instagram_login_mode == "instagram_login":
            response = await self._client.get(
                "/me",
                params={"fields": "user_id,username,name"},
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data")
            if isinstance(data, list) and data:
                payload = data[0]
            account_id = str(payload.get("user_id") or payload.get("id") or "")
            return {
                **payload,
                "id": account_id,
                "name": payload.get("name") or payload.get("username") or account_id,
            }
        response = await self._client.get(
            f"/{self._external_account_id}",
            params={"fields": "id,name"},
        )
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
