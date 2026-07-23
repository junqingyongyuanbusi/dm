import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from social_reply.shared.config import get_settings

_DISPATCH_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="actor-dispatch")
_DISPATCH_CAPACITY = asyncio.Semaphore(8)


async def dispatch_actor(
    actor: Any,
    *args: Any,
    inline: Callable[[], Awaitable[Any]] | None = None,
    timeout_seconds: float | None = None,
) -> Any:
    """Dispatch an actor, optionally executing its async implementation in tests."""
    if get_settings().testing:
        if inline is not None:
            return await inline()
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds if timeout_seconds is not None else None
    if deadline is None:
        await _DISPATCH_CAPACITY.acquire()
    else:
        await asyncio.wait_for(
            _DISPATCH_CAPACITY.acquire(),
            timeout=max(0.001, deadline - loop.time()),
        )
    try:
        pending = loop.run_in_executor(_DISPATCH_EXECUTOR, partial(actor.send, *args))
    except Exception:
        _DISPATCH_CAPACITY.release()
        raise
    pending.add_done_callback(lambda _future: _DISPATCH_CAPACITY.release())
    if deadline is None:
        await pending
    else:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(asyncio.shield(pending), timeout=remaining)
    return None
