from collections.abc import Awaitable, Callable
from typing import Any

from social_reply.shared.config import get_settings


async def dispatch_actor(
    actor: Any,
    *args: Any,
    inline: Callable[[], Awaitable[Any]] | None = None,
) -> Any:
    """Dispatch an actor, optionally executing its async implementation in tests."""
    if get_settings().testing:
        if inline is not None:
            return await inline()
        return None
    actor.send(*args)
    return None
