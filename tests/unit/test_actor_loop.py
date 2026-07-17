import asyncio

from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop


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
