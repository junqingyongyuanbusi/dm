"""本地冒烟：以 Chatwoot 签名格式向本地 API 发送一条模拟 message_created"""

import hashlib
import hmac
import json
import sys
import time

import httpx

SECRET = sys.argv[1] if len(sys.argv) > 1 else "change-me"
payload = {
    "event": "message_created", "id": int(time.time()), "content": "冒烟测试：可以提现吗？",
    "message_type": "incoming", "private": False,
    "created_at": "2026-07-14T10:00:00Z",
    "sender": {"id": 9, "type": "contact", "name": "冒烟用户"},
    "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
    "account": {"id": 1},
}
body = json.dumps(payload).encode()
ts = str(int(time.time()))
digest = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
resp = httpx.post(
    # 用 127.0.0.1 而非 localhost：macOS 双栈下 localhost 可能先解析到 IPv6 ::1，
    # 而 uvicorn 默认仅绑 IPv4 127.0.0.1，会连接失败（Task 9 端到端冒烟实测）
    "http://127.0.0.1:8000/webhooks/chatwoot",
    content=body,
    headers={
        "X-Chatwoot-Signature": f"sha256={digest}",
        "X-Chatwoot-Timestamp": ts,
        "Content-Type": "application/json",
    },
)
print(resp.status_code, resp.text)
