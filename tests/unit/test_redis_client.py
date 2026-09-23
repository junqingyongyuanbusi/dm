import pytest
from redis.retry import Retry

from social_reply.infrastructure.redis_client import (
    REDIS_HEALTH_CHECK_INTERVAL_SECONDS,
    REDIS_RETRY_ATTEMPTS,
    REDIS_SOCKET_TIMEOUT_SECONDS,
    make_async_redis_client,
    make_redis_retry,
    make_sync_redis_client,
)


def test_shared_retry_uses_exponential_backoff():
    retry = make_redis_retry()

    assert retry.get_retries() == REDIS_RETRY_ATTEMPTS


def test_sync_client_applies_shared_hardening():
    client = make_sync_redis_client("redis://localhost:6379/0")

    connection_kwargs = client.connection_pool.connection_kwargs
    assert connection_kwargs["socket_connect_timeout"] == REDIS_SOCKET_TIMEOUT_SECONDS
    assert connection_kwargs["socket_timeout"] == REDIS_SOCKET_TIMEOUT_SECONDS
    assert connection_kwargs["health_check_interval"] == REDIS_HEALTH_CHECK_INTERVAL_SECONDS
    assert isinstance(connection_kwargs["retry"], Retry)
    assert connection_kwargs["retry"].get_retries() == REDIS_RETRY_ATTEMPTS


async def test_async_client_applies_shared_hardening():
    client = make_async_redis_client("redis://localhost:6379/0")

    connection_kwargs = client.connection_pool.connection_kwargs
    assert connection_kwargs["socket_connect_timeout"] == REDIS_SOCKET_TIMEOUT_SECONDS
    assert connection_kwargs["socket_timeout"] == REDIS_SOCKET_TIMEOUT_SECONDS
    assert connection_kwargs["health_check_interval"] == REDIS_HEALTH_CHECK_INTERVAL_SECONDS
    assert isinstance(connection_kwargs["retry"], Retry)
    assert connection_kwargs["retry"].get_retries() == REDIS_RETRY_ATTEMPTS


@pytest.mark.parametrize("make_client", [make_sync_redis_client, make_async_redis_client])
def test_url_timeout_options_are_rejected(make_client):
    conflicting_url = "redis://localhost:6379/0?socket_timeout=9"

    with pytest.raises(ValueError, match="redis_url_timeout_options_not_allowed"):
        make_client(conflicting_url)


async def test_async_client_closes_cleanly_without_connecting():
    client = make_async_redis_client("redis://localhost:6379/0")

    await client.aclose()
