"""Shared Redis client factory with uniform timeouts, retries and health checks.

Production incidents showed transient Redis connection failures
(``BrokenPipeError``, ``Timeout reading/writing from socket``) between the API,
worker and the managed Redis instance. Clients created through a bare
``from_url`` propagated those failures immediately, which for the reply
pipeline means the kill switch check fails closed and decisions downgrade to
drafts. This factory centralises the connection hardening so every caller gets
the same protections:

- bounded socket timeouts so a single command cannot hang a request;
- exponential-backoff retries that absorb transient blips (fail-closed
  semantics are unchanged: exhausted retries still raise);
- periodic health checks so pooled connections silently dropped by network
  equipment are verified before reuse.
"""

from urllib.parse import parse_qs, urlsplit

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

from social_reply.shared.config import get_settings

REDIS_SOCKET_TIMEOUT_SECONDS = 3.0
REDIS_HEALTH_CHECK_INTERVAL_SECONDS = 30
REDIS_RETRY_ATTEMPTS = 2
REDIS_RETRY_BACKOFF_CAP_SECONDS = 0.5
REDIS_RETRY_BACKOFF_BASE_SECONDS = 0.05

_REDIS_TIMEOUT_QUERY_OPTIONS = {"socket_connect_timeout", "socket_timeout"}


def _validate_redis_url(url: str) -> str:
    configured_options = set(parse_qs(urlsplit(url).query))
    conflicts = configured_options & _REDIS_TIMEOUT_QUERY_OPTIONS
    if conflicts:
        names = ",".join(sorted(conflicts))
        raise ValueError(f"redis_url_timeout_options_not_allowed:{names}")
    return url


def make_redis_retry() -> Retry:
    return Retry(
        backoff=ExponentialBackoff(
            cap=REDIS_RETRY_BACKOFF_CAP_SECONDS,
            base=REDIS_RETRY_BACKOFF_BASE_SECONDS,
        ),
        retries=REDIS_RETRY_ATTEMPTS,
    )


def make_sync_redis_client(url: str | None = None) -> Redis:
    return Redis.from_url(
        _validate_redis_url(url if url is not None else get_settings().redis_url),
        socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        retry=make_redis_retry(),
        health_check_interval=REDIS_HEALTH_CHECK_INTERVAL_SECONDS,
    )


def make_async_redis_client(url: str | None = None) -> AsyncRedis:
    return AsyncRedis.from_url(
        _validate_redis_url(url if url is not None else get_settings().redis_url),
        socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        retry=make_redis_retry(),
        health_check_interval=REDIS_HEALTH_CHECK_INTERVAL_SECONDS,
    )
