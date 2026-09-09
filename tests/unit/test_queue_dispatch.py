import asyncio
import threading
from unittest.mock import Mock

import dramatiq
import pytest
from dramatiq.brokers.stub import StubBroker
from pydantic import ValidationError

from social_reply.infrastructure.queue import broker as broker_module
from social_reply.infrastructure.queue import dispatch
from social_reply.shared.config import Settings


def test_test_environment_uses_stub_broker():
    assert isinstance(dramatiq.get_broker(), StubBroker)
    assert broker_module.broker is dramatiq.get_broker()


def test_production_broker_rejects_timeout_query_overrides(monkeypatch):
    monkeypatch.setattr(
        broker_module,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "testing": False,
                "redis_url": "redis://queue.example/0?socket_timeout=30",
            },
        )(),
    )
    with pytest.raises(ValueError, match="redis_url_timeout_options_not_allowed:socket_timeout"):
        broker_module.setup_broker()


@pytest.mark.parametrize("namespace", [None, "fresh_queue-2026"])
def test_production_broker_configures_redis_timeouts_and_namespace(monkeypatch, namespace):
    captured = {}
    redis_client = object()
    redis_broker = object()
    monkeypatch.delenv("DRAMATIQ_NAMESPACE", raising=False)
    namespace_options = {} if namespace is None else {"dramatiq_namespace": namespace}
    settings = Settings(_env_file=None, testing=True, **namespace_options)
    expected_namespace = "dramatiq" if namespace is None else namespace
    assert settings.dramatiq_namespace == expected_namespace

    class FakeRedis:
        @classmethod
        def from_url(cls, url, **kwargs):
            captured["redis"] = (url, kwargs)
            return redis_client

    monkeypatch.setattr(
        broker_module,
        "get_settings",
        lambda: settings.model_copy(
            update={"testing": False, "redis_url": "redis://queue.example/0"}
        ),
    )
    monkeypatch.setattr(broker_module, "Redis", FakeRedis)
    broker_factory = Mock(return_value=redis_broker)
    monkeypatch.setattr(broker_module, "RedisBroker", broker_factory)
    monkeypatch.setattr(
        broker_module.dramatiq,
        "set_broker",
        lambda value: captured.setdefault("broker", value),
    )

    assert broker_module.setup_broker() is redis_broker
    broker_factory.assert_called_once_with(client=redis_client, namespace=expected_namespace)
    assert captured["broker"] is redis_broker
    assert captured["redis"] == (
        "redis://queue.example/0",
        {"socket_connect_timeout": 3, "socket_timeout": 3},
    )


@pytest.mark.parametrize("namespace", ["A", "a" * 64, "Fresh_queue-2026"])
def test_dramatiq_namespace_accepts_valid_boundaries(namespace):
    settings = Settings(_env_file=None, testing=True, dramatiq_namespace=namespace)
    assert settings.dramatiq_namespace == namespace


@pytest.mark.parametrize(
    "namespace",
    [
        "",
        " ",
        " fresh",
        "fresh ",
        "fresh\n",
        "fresh\nqueue",
        "fresh\tqueue",
        "a" * 65,
        "fresh:queue",
        "fresh/*",
        "{fresh}",
        "fresh\x00",
        "\u961f\u5217",
    ],
)
def test_dramatiq_namespace_rejects_invalid_values(namespace):
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, testing=True, dramatiq_namespace=namespace)

    assert any(detail["loc"] == ("dramatiq_namespace",) for detail in error.value.errors())


def test_custom_namespace_does_not_change_stub_broker(monkeypatch):
    settings = Settings(_env_file=None, testing=True, dramatiq_namespace="fresh_queue")
    redis_factory = Mock(side_effect=AssertionError("StubBroker must not create Redis"))
    broker_factory = Mock(side_effect=AssertionError("StubBroker must not create RedisBroker"))
    register_broker = Mock()
    monkeypatch.setattr(broker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(broker_module.Redis, "from_url", redis_factory)
    monkeypatch.setattr(broker_module, "RedisBroker", broker_factory)
    monkeypatch.setattr(broker_module.dramatiq, "set_broker", register_broker)

    configured_broker = broker_module.setup_broker()

    assert isinstance(configured_broker, StubBroker)
    register_broker.assert_called_once_with(configured_broker)
    redis_factory.assert_not_called()
    broker_factory.assert_not_called()


async def test_testing_dispatch_runs_inline_without_actor_send(monkeypatch):
    calls: list[str] = []

    class Actor:
        def send(self, *_args):
            raise AssertionError("testing dispatch must not call actor.send")

    async def inline():
        calls.append("inline")
        return "processed"

    monkeypatch.setattr(
        dispatch,
        "get_settings",
        lambda: type("Settings", (), {"testing": True})(),
    )

    assert await dispatch.dispatch_actor(Actor(), "job-1", inline=inline) == "processed"
    assert calls == ["inline"]


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


async def test_production_dispatch_releases_capacity_when_actor_send_fails(monkeypatch):
    class BrokenActor:
        def send(self, *_args):
            raise RuntimeError("broker unavailable")

    class HealthyActor:
        def send(self, *_args):
            return None

    monkeypatch.setattr(
        dispatch,
        "get_settings",
        lambda: type("Settings", (), {"testing": False})(),
    )
    monkeypatch.setattr(dispatch, "_DISPATCH_CAPACITY", asyncio.Semaphore(1))

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await dispatch.dispatch_actor(BrokenActor(), "first")
    assert await dispatch.dispatch_actor(HealthyActor(), "second", timeout_seconds=0.5) is None


async def test_production_dispatch_rejects_work_beyond_capacity(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = []

    class Actor:
        def send(self, value):
            calls.append(value)
            started.set()
            release.wait(timeout=1)

    monkeypatch.setattr(
        dispatch,
        "get_settings",
        lambda: type("Settings", (), {"testing": False})(),
    )
    monkeypatch.setattr(dispatch, "_DISPATCH_CAPACITY", asyncio.Semaphore(1))
    first = asyncio.create_task(dispatch.dispatch_actor(Actor(), "first"))
    while not started.is_set():
        await asyncio.sleep(0.001)
    try:
        with pytest.raises(TimeoutError):
            await dispatch.dispatch_actor(Actor(), "second", timeout_seconds=0.01)
        assert calls == ["first"]
    finally:
        release.set()
        await first


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
        await asyncio.sleep(0.01)
