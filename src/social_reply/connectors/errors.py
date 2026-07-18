"""平台发送的领域异常:区分「永久失败」与「暂时失败」。

投递层(outbox)据此决定终态:永久失败标 NEEDS_REVIEW 并停止重试,暂时失败退避后重试。
平台特定的业务错误(X 349「对方不收 DM」、Meta 10「超出 24h 窗口」、190「token 失效」)
在 HTTP 层可能是 2xx 或 4xx,单看状态码无法判定是否值得重试,故由各 client 解析后
抛出这些语义异常,把「为什么发不出去」显式传达给投递层与后台运营。
"""


class PlatformSendError(Exception):
    """所有平台发送错误的基类,携带面向运营的错误码与原始消息。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}:{message}" if message else code)


class PermanentSendError(PlatformSendError):
    """重试也不会成功:对方不可达/未关注/拉黑、超出消息窗口、权限或 scope 不足、token 失效。

    消息确定未送达(非歧义),投递层应直接标 NEEDS_REVIEW 并停止重试,把 code 暴露给运营。
    """


class RetryableSendError(PlatformSendError):
    """暂时性失败:限流(429 / Graph 613)。投递层应指数退避后重试。"""
