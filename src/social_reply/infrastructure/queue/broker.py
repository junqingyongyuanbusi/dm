from urllib.parse import parse_qs, urlsplit

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.brokers.stub import StubBroker
from redis import Redis

from social_reply.shared.config import get_settings

_REDIS_SOCKET_TIMEOUT_SECONDS = 3
_REDIS_TIMEOUT_QUERY_OPTIONS = {"socket_connect_timeout", "socket_timeout"}


def _validate_redis_url(url: str) -> str:
    configured_options = set(parse_qs(urlsplit(url).query))
    conflicts = configured_options & _REDIS_TIMEOUT_QUERY_OPTIONS
    if conflicts:
        names = ",".join(sorted(conflicts))
        raise ValueError(f"redis_url_timeout_options_not_allowed:{names}")
    return url


def setup_broker() -> dramatiq.Broker:
    settings = get_settings()
    if settings.testing:
        broker = StubBroker()
    else:
        client = Redis.from_url(
            _validate_redis_url(settings.redis_url),
            socket_connect_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
        )
        broker = RedisBroker(client=client)
    dramatiq.set_broker(broker)
    return broker


broker = setup_broker()
