"""Outbox 补扫 scheduler 入口：uv run python -m apps.scheduler.main（30s 一轮常驻循环）"""

import time

from social_reply.application.message_delivery.sweep import sweep_outbox
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop

_INTERVAL_SECONDS = 30


def main() -> None:
    while True:
        enqueued = run_on_actor_loop(sweep_outbox())
        if enqueued:
            print(f"sweep_outbox: 重新入队 {len(enqueued)} 条")  # noqa: T201
        time.sleep(_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
