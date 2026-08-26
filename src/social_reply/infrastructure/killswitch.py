from typing import Protocol

import redis.asyncio as aioredis

from social_reply.shared.config import get_settings


class _RedisLike(Protocol):
    async def mget(self, keys: list[str]) -> list[bytes | None]: ...


class KillSwitchChecker:
    """全局 / 品牌 / 账号 三级自动回复急停。
    任一层标志位存在即视为禁用——秒级停发的最后一道产品级开关。"""

    def __init__(self, redis: _RedisLike):
        self._redis = redis

    async def is_disabled(self, brand_id: str, account_id: str, tenant_id: str = "default") -> bool:
        keys = [
            f"killswitch:global:{tenant_id}",
            f"killswitch:brand:{tenant_id}:{brand_id}",
            f"killswitch:account:{tenant_id}:{account_id}",
        ]
        values = await self._redis.mget(keys)
        return any(v is not None for v in values)


_redis = None


def make_killswitch_checker() -> KillSwitchChecker:
    """Return a process-wide checker so decision and delivery share one Redis pool."""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(get_settings().redis_url)
    return KillSwitchChecker(_redis)
