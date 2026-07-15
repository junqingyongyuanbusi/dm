"""Outbox 补扫 scheduler 入口：uv run python -m apps.scheduler.main（30s 一轮常驻循环）"""

import logging
import time

from social_reply.application.message_delivery.sweep import sweep_outbox
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop

_INTERVAL_SECONDS = 30

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    while True:
        # 单轮失败（DB 闪断 / actor loop 超时等）只记日志，不杀死 scheduler
        try:
            enqueued = run_on_actor_loop(sweep_outbox())
            if enqueued:
                logger.info("sweep_outbox: 重新入队 %d 条", len(enqueued))
        except Exception:  # noqa: BLE001
            logger.exception("sweep_outbox 本轮失败，%ds 后重试", _INTERVAL_SECONDS)
        time.sleep(_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
