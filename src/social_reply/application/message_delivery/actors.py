import dramatiq

import social_reply.infrastructure.queue.broker  # noqa: F401  确保 broker 先初始化
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop


@dramatiq.actor(max_retries=3)
def deliver_outbox_message(outbox_id: str) -> None:
    # 注意：deliver_outbox 对歧义超时（NEEDS_REVIEW）与可重试失败（FAILED）均正常返回，
    # 业务重试走 FAILED + 补扫；max_retries=3 只覆盖基础设施级异常（如 DB 连不上），
    # 不能靠 Dramatiq 盲重试文本消息。
    from social_reply.application.message_delivery.outbox import deliver_outbox

    run_on_actor_loop(deliver_outbox(outbox_id))
