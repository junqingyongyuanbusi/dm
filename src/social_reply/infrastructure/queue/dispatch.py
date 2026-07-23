import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from social_reply.shared.config import get_settings

_DISPATCH_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="actor-dispatch")


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
    pending = loop.run_in_executor(_DISPATCH_EXECUTOR, partial(actor.send, *args))
    if timeout_seconds is None:
        await pending
    else:
        await asyncio.wait_for(pending, timeout=timeout_seconds)
    return None
