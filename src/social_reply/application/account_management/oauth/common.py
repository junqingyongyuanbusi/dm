"""OAuth 接入流共享设施:加密 state cookie、统一提示页、回调 URL。

state cookie 是各平台授权流的安全承重件:callback 由平台 302 跳回,属跨站顶级
导航,admin 会话 cookie(SameSite=Strict)不随行,授权凭据即这枚 Fernet 加密的
state 本身(内含发起者与租户上下文)。SameSite=Lax、10 分钟 TTL、用后即删。
"""

import time

from fastapi import Request, Response
from fastapi.responses import HTMLResponse

from social_reply.application.account_management.admin import _page, _secure_cookie, html
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle, encrypt_secret_bundle
from social_reply.shared.config import get_settings

STATE_TTL_SECONDS = 600


def admin_callback_url(path: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}{path}"


def write_state(
    response: Response, request: Request, cookie_name: str, payload: dict[str, str]
) -> None:
    envelope = encrypt_secret_bundle({**payload, "ts": str(int(time.time()))})
    response.set_cookie(
        cookie_name,
        envelope["__encrypted__"],
        max_age=STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(request),
    )


def read_state(request: Request, cookie_name: str) -> dict[str, str] | None:
    """缺失/篡改/过期一律返回 None,调用方给统一的「重新发起」提示。"""
    cookie = request.cookies.get(cookie_name)
    if not cookie:
        return None
    try:
        state = decrypt_secret_bundle({"__encrypted__": cookie})
    except ValueError:
        return None
    if int(state.get("ts") or 0) + STATE_TTL_SECONDS < int(time.time()):
        return None
    return state


def notice(title: str, message: str, *, status_code: int = 200) -> HTMLResponse:
    body = f"""<a class="back" href="/admin/accounts">← 返回账号页</a>
<section class="card"><h1 style="font-size:24px">{html.escape(title)}</h1>
<p>{html.escape(message)}</p></section>"""
    return HTMLResponse(_page(title, body, active="accounts"), status_code=status_code)
