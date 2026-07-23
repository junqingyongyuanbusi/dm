import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.brokers.stub import StubBroker
from redis import Redis

from social_reply.shared.config import get_settings

_REDIS_SOCKET_TIMEOUT_SECONDS = 3


def setup_broker() -> dramatiq.Broker:
    settings = get_settings()
    if settings.testing:
        broker = StubBroker()
    else:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
        )
        broker = RedisBroker(client=client)
    dramatiq.set_broker(broker)
    return broker


broker = setup_broker()
