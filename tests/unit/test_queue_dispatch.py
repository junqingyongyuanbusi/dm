import dramatiq
from dramatiq.brokers.stub import StubBroker

from social_reply.infrastructure.queue import broker as broker_module


def test_test_environment_uses_stub_broker():
    assert isinstance(dramatiq.get_broker(), StubBroker)
    assert broker_module.broker is dramatiq.get_broker()
