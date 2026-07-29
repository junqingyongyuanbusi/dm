import httpx

from social_reply.connectors.xchat.client import XChatClient
from social_reply.connectors.xchat.setup import (
    XChatKeyConfigurationError,
    XChatKeyUnlockError,
    unlock_xchat_private_keys,
)


class XChatActivationError(ValueError):
    def __init__(
        self,
        code: str,
        operator_message: str,
        *,
        status_code: int = 422,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.operator_message = operator_message
        self.status_code = status_code
        self.retryable = retryable


def _response_error_type(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    return str(payload.get("type") or "") if isinstance(payload, dict) else ""


def _http_activation_error(exc: httpx.HTTPStatusError) -> XChatActivationError:
    status = exc.response.status_code
    error_type = _response_error_type(exc.response)
    if status == 401:
        return XChatActivationError(
            "XCHAT_REAUTHORIZATION_REQUIRED",
            "该账号的 X OAuth 凭据已失效，请撤销旧 App 授权后重新完成 OAuth。",
        )
    if status == 403 and error_type.endswith("/oauth1-permissions"):
        return XChatActivationError(
            "XCHAT_DM_PERMISSION_REQUIRED",
            "当前 X App 没有可用的 Direct Message 权限。请确认 Railway 使用的 "
            "Consumer Key 对应 App 已设置 Read and write and Direct message，"
            "然后撤销并重新授权该账号。",
        )
    if status == 403:
        return XChatActivationError(
            "XCHAT_ACCESS_FORBIDDEN",
            "X 拒绝读取该账号的 XChat 密钥。请确认账号已启用 XChat，且当前 App "
            "和 OAuth 授权允许访问私信。",
        )
    if status == 404:
        return XChatActivationError(
            "XCHAT_NOT_ENABLED",
            "X 没有返回该账号的 XChat 密钥。请先在 X 客户端启用 XChat 并设置 4 位 PIN。",
        )
    if status == 429:
        return XChatActivationError(
            "XCHAT_RATE_LIMITED",
            "X API 当前限流，请等待几分钟后再提交 PIN。",
            status_code=429,
            retryable=True,
        )
    if status >= 500:
        return XChatActivationError(
            "XCHAT_API_UNAVAILABLE",
            "XChat 密钥服务暂时不可用，请稍后重试。",
            status_code=503,
            retryable=True,
        )
    return XChatActivationError(
        "XCHAT_API_ERROR",
        "X 返回了无法处理的 XChat 密钥响应，请稍后重试或检查账号授权。",
        status_code=502,
    )


async def unlock_account_xchat_keys(
    *,
    client: XChatClient,
    user_id: str,
    pin: str,
    records: list[dict] | None = None,
) -> tuple[str, str]:
    try:
        return await unlock_xchat_private_keys(
            client=client,
            user_id=user_id,
            pin=pin,
            records=records,
        )
    except httpx.HTTPStatusError as exc:
        raise _http_activation_error(exc) from exc
    except httpx.TransportError as exc:
        raise XChatActivationError(
            "XCHAT_API_UNAVAILABLE",
            "XChat 密钥服务暂时不可用，请稍后重新提交 PIN。",
            status_code=503,
            retryable=True,
        ) from exc
    except XChatKeyConfigurationError as exc:
        raise XChatActivationError(
            "XCHAT_KEY_CONFIG_INVALID",
            "X 返回的 Juicebox 密钥配置无效，请稍后重新提交 PIN。",
            status_code=502,
        ) from exc
    except XChatKeyUnlockError as exc:
        if exc.reason == "invalid_pin":
            raise XChatActivationError(
                "XCHAT_PIN_INVALID",
                "PIN 不正确。请输入当前账号在 X 中设置的最新 4 位 XChat PIN。",
            ) from exc
        if exc.reason == "not_registered":
            raise XChatActivationError(
                "XCHAT_NOT_ENABLED",
                "该账号的 XChat 密钥尚未注册。请先在 X 客户端启用 XChat 并设置 PIN。",
            ) from exc
        if exc.reason == "invalid_auth":
            raise XChatActivationError(
                "XCHAT_KEYSTORE_AUTH_INVALID",
                "XChat 密钥存储授权已失效。请重新 OAuth 授权账号后再次提交 PIN。",
            ) from exc
        if exc.reason == "upgrade_required":
            raise XChatActivationError(
                "XCHAT_SDK_UPGRADE_REQUIRED",
                "X 要求更新 XChat SDK，当前服务版本暂时无法恢复密钥。",
                status_code=503,
            ) from exc
        if exc.reason == "rate_limited":
            raise XChatActivationError(
                "XCHAT_KEYSTORE_RATE_LIMITED",
                "XChat 密钥恢复尝试过于频繁，请等待几分钟后重新提交 PIN。",
                status_code=429,
                retryable=True,
            ) from exc
        if exc.reason == "temporarily_unavailable":
            raise XChatActivationError(
                "XCHAT_KEYSTORE_UNAVAILABLE",
                "XChat 密钥存储暂时不可用，请稍后重新提交 PIN。",
                status_code=503,
                retryable=True,
            ) from exc
        raise XChatActivationError(
            "XCHAT_PIN_RECOVERY_FAILED",
            "XChat 密钥恢复失败。请确认 PIN 属于当前账号；问题持续存在时请稍后重试。",
        ) from exc
    except ValueError as exc:
        if str(exc) == "xchat_public_keys_not_found":
            raise XChatActivationError(
                "XCHAT_NOT_ENABLED",
                "该账号没有可用的 XChat 公钥。请先在 X 客户端启用 XChat 并设置 4 位 PIN。",
            ) from exc
        if str(exc) == "xchat_public_key_record_incomplete":
            raise XChatActivationError(
                "XCHAT_KEY_RECORD_INVALID",
                "X 返回的 XChat 密钥配置不完整，请稍后重试。",
                status_code=502,
            ) from exc
        raise XChatActivationError(
            "XCHAT_KEY_RECOVERY_ERROR",
            "XChat 密钥恢复返回了未知错误，请稍后重新提交 PIN。",
            status_code=502,
        ) from exc
