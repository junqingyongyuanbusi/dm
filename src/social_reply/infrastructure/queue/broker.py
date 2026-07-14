import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.brokers.stub import StubBroker

from social_reply.shared.config import get_settings


def setup_broker() -> dramatiq.Broker:
    settings = get_settings()
    if settings.testing:
        broker = StubBroker()
    else:
        broker = RedisBroker(url=settings.redis_url)
    dramatiq.set_broker(broker)
    return broker


broker = setup_broker()
