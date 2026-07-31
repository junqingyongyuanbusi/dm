import asyncio
import threading

from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop, submit_on_actor_loop


def test_submit_on_actor_loop_is_nonblocking_and_returns_future():
    gate = asyncio.Event()

    async def _coro():
        await gate.wait()
        return 42

    future = submit_on_actor_loop(_coro())
    assert not future.done()
    run_on_actor_loop(_release(gate))
    assert future.result(timeout=1) == 42


async def _release(gate: asyncio.Event) -> None:
    gate.set()


def test_submit_on_actor_loop_future_captures_exception():
    async def _boom():
        raise ValueError("submit boom")

    future = submit_on_actor_loop(_boom())
    try:
        future.result(timeout=1)
        raise AssertionError("should have raised")
    except ValueError as exc:
        assert str(exc) == "submit boom"


def test_submit_on_actor_loop_done_callback_observes_result_and_exception():
    callbacks: list[object] = []
    completed = threading.Event()

    async def _ok():
        return 42

    async def _boom():
        raise ValueError("callback boom")

    def record(future):
        try:
            callbacks.append(future.result())
        except Exception as exc:  # noqa: BLE001 - the callback records the submitted outcome
            callbacks.append(exc)
        if len(callbacks) == 2:
            completed.set()

    success = submit_on_actor_loop(_ok())
    failure = submit_on_actor_loop(_boom())
    success.add_done_callback(record)
    failure.add_done_callback(record)

    assert completed.wait(timeout=1)
    assert 42 in callbacks
    assert any(
        isinstance(value, ValueError) and str(value) == "callback boom" for value in callbacks
    )


def test_run_on_actor_loop_executes_coroutine():
    async def _coro():
        await asyncio.sleep(0)
        return 42

    assert run_on_actor_loop(_coro()) == 42


def test_run_on_actor_loop_propagates_exception():
    async def _boom():
        raise ValueError("boom")

    try:
        run_on_actor_loop(_boom())
        raise AssertionError("should have raised")
    except ValueError as e:
        assert str(e) == "boom"


def test_same_loop_reused_across_calls():
    async def _which_loop():
        return id(asyncio.get_running_loop())

    assert run_on_actor_loop(_which_loop()) == run_on_actor_loop(_which_loop())
