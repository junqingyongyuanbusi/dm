import asyncio
import threading

import dramatiq

import social_reply.infrastructure.queue.broker  # noqa: F401  确保 broker 先初始化

# 常驻事件循环：单例引擎的连接池绑定事件循环，
# 每条消息 asyncio.run() 会跨循环复用连接导致 "Event loop is closed"（Task 1 质量评审实测）
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True, name="actor-loop").start()


@dramatiq.actor(max_retries=3)
def process_chatwoot_event(raw_event_id: str) -> None:
    from social_reply.application.event_ingestion.processor import process_raw_event

    future = asyncio.run_coroutine_threadsafe(process_raw_event(raw_event_id), _loop)
    try:
        # 无超时的 result() 会阻塞在 C 层锁上，Dramatiq TimeLimit 杀不掉（评审核对源码）
        future.result(timeout=120)
    except TimeoutError:
        future.cancel()
        raise
