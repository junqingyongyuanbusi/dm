import threading

import dramatiq
import pytest
from dramatiq.brokers.stub import StubBroker

from social_reply.infrastructure.queue import broker as broker_module
from social_reply.infrastructure.queue import dispatch


def test_test_environment_uses_stub_broker():
    assert isinstance(dramatiq.get_broker(), StubBroker)
    assert broker_module.broker is dramatiq.get_broker()


def test_production_broker_configures_redis_timeouts(monkeypatch):
    captured = {}
    redis_client = object()
    redis_broker = object()

    class FakeRedis:
        @classmethod
        def from_url(cls, url, **kwargs):
            captured["redis"] = (url, kwargs)
            return redis_client

    monkeypatch.setattr(
        broker_module,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {"testing": False, "redis_url": "redis://queue.example/0"},
        )(),
    )
    monkeypatch.setattr(broker_module, "Redis", FakeRedis)
    monkeypatch.setattr(
        broker_module,
        "RedisBroker",
        lambda *, client: redis_broker if client is redis_client else None,
    )
    monkeypatch.setattr(
        broker_module.dramatiq,
        "set_broker",
        lambda value: captured.setdefault("broker", value),
    )

    assert broker_module.setup_broker() is redis_broker
    assert captured["broker"] is redis_broker
    assert captured["redis"] == (
        "redis://queue.example/0",
        {"socket_connect_timeout": 3, "socket_timeout": 3},
    )


async def test_production_dispatch_runs_actor_send_off_loop(monkeypatch):
    caller_thread = threading.get_ident()
    calls = []

    class Actor:
        def send(self, *args):
            calls.append((args, threading.get_ident()))

    monkeypatch.setattr(
        dispatch,
        "get_settings",
        lambda: type("Settings", (), {"testing": False})(),
    )
    assert await dispatch.dispatch_actor(Actor(), "job-1", timeout_seconds=0.5) is None
    assert calls[0][0] == ("job-1",)
    assert calls[0][1] != caller_thread


async def test_production_dispatch_timeout_is_bounded(monkeypatch):
    release = threading.Event()

    class Actor:
        def send(self, *_args):
            release.wait(timeout=1)

    monkeypatch.setattr(
        dispatch,
        "get_settings",
        lambda: type("Settings", (), {"testing": False})(),
    )
    try:
        with pytest.raises(TimeoutError):
            await dispatch.dispatch_actor(Actor(), "job-1", timeout_seconds=0.01)
    finally:
        release.set()
