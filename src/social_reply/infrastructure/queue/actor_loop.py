import asyncio
import threading
from collections.abc import Coroutine
from typing import Any

# 单一常驻事件循环：所有 Dramatiq actor 共用，使单例引擎连接池只绑定这一个循环。
# 每消息 asyncio.run() 会跨循环复用 asyncpg 连接导致 "Event loop is closed"。
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True, name="actor-loop").start()


def run_on_actor_loop(coro: Coroutine[Any, Any, Any], timeout: float = 120) -> Any:
    """在常驻 loop 上跑协程并阻塞取结果。
    无超时的 result() 会阻塞在 C 层锁上，Dramatiq TimeLimit 杀不掉
    """
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise
