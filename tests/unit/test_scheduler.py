import asyncio
import logging
from concurrent.futures import Future

from apps.scheduler import main as scheduler
from social_reply.shared.config import Settings


def _settings(**updates: object) -> Settings:
    values = {
        "testing": True,
        "chatwoot_enabled": False,
        "platform_secret_keys": "Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM=",
        **updates,
    }
    return Settings(_env_file=None, **values)


def test_build_specs_uses_feature_flags_and_each_settings_snapshot():
    first = _settings(
        chatwoot_enabled=True,
        scheduler_core_interval_seconds=2.5,
        scheduler_core_warn_after_seconds=11,
        scheduler_inspection_warn_after_seconds=22,
        chatwoot_reconcile_interval_seconds=7,
        x_dm_poll_interval_seconds=13,
        x_webhook_check_interval_seconds=17,
        xchat_poll_interval_seconds=19,
        xchat_subscription_check_interval_seconds=23,
        xchat_recovery_sweep_interval_seconds=29,
    )
    second = first.model_copy(
        update={
            "scheduler_core_interval_seconds": 5,
            "chatwoot_reconcile_interval_seconds": 31,
            "xchat_recovery_sweep_interval_seconds": 37,
        }
    )

    first_spec_list = scheduler._build_sweep_specs(first)
    first_specs = {spec.name: spec for spec in first_spec_list}
    second_specs = {spec.name: spec for spec in scheduler._build_sweep_specs(second)}

    first_inspection_index = next(
        index for index, spec in enumerate(first_spec_list) if spec.lane == "inspection"
    )
    assert all(spec.lane == "core" for spec in first_spec_list[:first_inspection_index])
    assert {name for name, spec in first_specs.items() if spec.lane == "core"} == {
        "sweep_provisioning_jobs",
        "sweep_initial_raw_events",
        "sweep_decision_jobs",
        "sweep_outbox",
        "sweep_xchat_recovery",
    }
    for name in (
        "sweep_provisioning_jobs",
        "sweep_initial_raw_events",
        "sweep_decision_jobs",
        "sweep_outbox",
    ):
        assert first_specs[name].interval_seconds == 2.5
        assert first_specs[name].warn_after_seconds == 11
        assert second_specs[name].interval_seconds == 5
    assert first_specs["sweep_xchat_recovery"].lane == "core"
    assert first_specs["sweep_xchat_recovery"].interval_seconds == 29
    assert first_specs["sweep_xchat_recovery"].warn_after_seconds == 11
    assert first_specs["reconcile_chatwoot_messages"].interval_seconds == 7
    assert second_specs["reconcile_chatwoot_messages"].interval_seconds == 31
    assert first_specs["poll_x_direct_messages"].interval_seconds == 13
    assert first_specs["ensure_x_webhooks_valid"].interval_seconds == 17
    assert first_specs["poll_xchat_messages"].interval_seconds == 19
    assert first_specs["ensure_xchat_subscriptions"].interval_seconds == 23
    assert all(
        spec.warn_after_seconds == 22 for spec in first_specs.values() if spec.lane == "inspection"
    )
    assert second_specs["sweep_xchat_recovery"].interval_seconds == 37
    assert not hasattr(scheduler, "_SWEEPS")


def test_build_specs_omits_disabled_integrations():
    specs = {
        spec.name
        for spec in scheduler._build_sweep_specs(
            _settings(
                x_legacy_dm_enabled=False,
                x_activity_enabled=False,
                xchat_enabled=False,
                facebook_messenger_enabled=False,
                instagram_messaging_enabled=False,
            )
        )
    }

    assert specs == {
        "sweep_provisioning_jobs",
        "sweep_initial_raw_events",
        "sweep_decision_jobs",
        "sweep_outbox",
    }


async def test_one_tick_submits_all_due_specs_and_isolates_exceptions(caplog):
    calls: list[str] = []

    async def broken() -> list[str]:
        calls.append("broken")
        raise RuntimeError("boom")

    async def healthy() -> list[str]:
        calls.append("healthy")
        return ["one"]

    specs = (
        scheduler.SweepSpec("broken", "inspection", 10, 5, broken),
        scheduler.SweepSpec("healthy", "core", 3, 5, healthy),
    )
    runtimes = scheduler._new_runtimes(specs, now=0)
    tasks: list[asyncio.Task[None]] = []

    def submit(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    with caplog.at_level(logging.INFO):
        scheduler._tick(specs, runtimes, now=0, submit=submit, clock=lambda: 2)
        await asyncio.gather(*tasks)

    assert calls == ["broken", "healthy"]
    assert all(not runtime.running for runtime in runtimes.values())
    records = [record for record in caplog.records if record.message == "scheduler sweep completed"]
    assert {(record.sweep_name, record.status) for record in records} == {
        ("broken", "failure"),
        ("healthy", "success"),
    }
    healthy_record = next(record for record in records if record.sweep_name == "healthy")
    assert healthy_record.lane == "core"
    assert healthy_record.duration == 0
    assert healthy_record.recovered_count == 1


async def test_due_intervals_coalesce_without_overlap_and_warn_once(caplog):
    release = asyncio.Event()
    calls = 0

    async def slow() -> list[str]:
        nonlocal calls
        calls += 1
        await release.wait()
        return []

    spec = scheduler.SweepSpec("slow", "inspection", 10, 5, slow)
    specs = (spec,)
    runtimes = scheduler._new_runtimes(specs, now=0)
    tasks: list[asyncio.Task[None]] = []

    def submit(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    with caplog.at_level(logging.WARNING):
        scheduler._tick(specs, runtimes, now=0, submit=submit, clock=lambda: 0)
        await asyncio.sleep(0)
        scheduler._tick(specs, runtimes, now=5, submit=submit, clock=lambda: 5)
        scheduler._tick(specs, runtimes, now=10, submit=submit, clock=lambda: 10)
        scheduler._tick(specs, runtimes, now=20, submit=submit, clock=lambda: 20)

    assert calls == 1
    assert len(tasks) == 1
    assert not tasks[0].cancelled()
    assert runtimes["slow"].running is True
    assert runtimes["slow"].next_due == 30
    warnings = [
        record
        for record in caplog.records
        if record.message == "scheduler sweep exceeded warning threshold"
    ]
    assert len(warnings) == 1
    assert warnings[0].sweep_name == "slow"
    assert warnings[0].lane == "inspection"
    assert warnings[0].max_instances == 1

    release.set()
    await tasks[0]
    assert runtimes["slow"].running is False
    assert runtimes["slow"].warned is False

    scheduler._tick(specs, runtimes, now=30, submit=submit, clock=lambda: 30)
    await tasks[1]
    scheduler._tick(specs, runtimes, now=31, submit=submit, clock=lambda: 31)

    assert calls == 2
    assert len(tasks) == 2


async def test_slow_inspection_does_not_block_repeated_core_runs():
    inspection_release = asyncio.Event()
    core_runs = 0

    async def core() -> list[str]:
        nonlocal core_runs
        core_runs += 1
        return []

    async def inspection() -> list[str]:
        await inspection_release.wait()
        return []

    specs = (
        scheduler.SweepSpec("core", "core", 3, 30, core),
        scheduler.SweepSpec("inspection", "inspection", 100, 300, inspection),
    )
    runtimes = scheduler._new_runtimes(specs, now=0)
    tasks: list[asyncio.Task[None]] = []

    def submit(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    for now in (0, 3, 6, 9):
        scheduler._tick(specs, runtimes, now=now, submit=submit, clock=lambda now=now: now)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert core_runs == 4
    assert runtimes["inspection"].running is True
    inspection_release.set()
    await asyncio.gather(*tasks)


def test_submission_failure_does_not_block_other_due_specs(caplog):
    submitted: list[str] = []

    async def run() -> list[str]:
        return []

    specs = (
        scheduler.SweepSpec("first", "core", 3, 30, run),
        scheduler.SweepSpec("second", "core", 3, 30, run),
    )
    runtimes = scheduler._new_runtimes(specs, now=0)

    def submit(coro):
        name = coro.cr_frame.f_locals["spec"].name
        submitted.append(name)
        if name == "first":
            raise RuntimeError("actor loop unavailable")
        coro.close()
        return Future()

    with caplog.at_level(logging.ERROR):
        scheduler._tick(specs, runtimes, now=0, submit=submit)

    assert submitted == ["first", "second"]
    assert runtimes["first"].running is False
    assert runtimes["first"].future is None
    assert "scheduler sweep submission failed" in caplog.text


def test_shutdown_drain_is_bounded_and_does_not_cancel_running_work(caplog):
    pending = Future()
    runtimes = {
        "core": scheduler.SweepRuntime(
            running=True,
            next_due=3,
            started_at=0,
            warned=False,
            future=pending,
        )
    }
    now = 0.0

    def clock():
        return now

    def sleep(seconds: float):
        nonlocal now
        now += seconds

    with caplog.at_level(logging.WARNING):
        scheduler._drain_running_sweeps(
            runtimes,
            timeout_seconds=0.1,
            clock=clock,
            sleep=sleep,
        )

    assert now >= 0.1
    assert not pending.cancelled()
    assert "scheduler shutdown grace period expired" in caplog.text


def test_main_reads_settings_once_and_uses_bounded_shutdown_drain(monkeypatch):
    settings = _settings(scheduler_tick_seconds=0.05)
    settings_calls = 0
    pending = Future()
    submitted = []

    async def run() -> list[str]:
        await asyncio.Event().wait()
        return []

    spec = scheduler.SweepSpec("core", "core", 3, 30, run)

    def get_settings_once():
        nonlocal settings_calls
        settings_calls += 1
        return settings

    class StopAfterOneTick:
        def __init__(self):
            self.checks = 0

        def is_set(self):
            self.checks += 1
            return self.checks > 1

        def wait(self, timeout):
            assert timeout == settings.scheduler_tick_seconds
            return True

        def set(self):
            return None

    def submit(coro):
        submitted.append(coro)
        return pending

    drained = []

    monkeypatch.setattr(scheduler, "get_settings", get_settings_once)
    monkeypatch.setattr(scheduler, "_build_sweep_specs", lambda snapshot: (spec,))
    monkeypatch.setattr(scheduler.threading, "Event", StopAfterOneTick)
    monkeypatch.setattr(scheduler.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(scheduler, "submit_on_actor_loop", submit)
    monkeypatch.setattr(scheduler, "_drain_running_sweeps", drained.append)

    scheduler.main()

    assert settings_calls == 1
    assert len(submitted) == 1
    assert len(drained) == 1
    assert not pending.cancelled()
    submitted[0].close()
