"""Recovery scheduler: uv run python -m apps.scheduler.main."""

import logging
import signal
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Literal

from social_reply.application.account_management.feishu_health import (
    reconcile_feishu_account_health,
)
from social_reply.application.account_management.jobs import sweep_provisioning_jobs
from social_reply.application.account_management.meta_health import reconcile_meta_account_health
from social_reply.application.event_ingestion.raw_recovery import sweep_initial_raw_events
from social_reply.application.event_ingestion.x_dm_poll import poll_x_direct_messages
from social_reply.application.event_ingestion.x_webhook_health import ensure_x_webhooks_valid
from social_reply.application.event_ingestion.xchat_poll import poll_xchat_messages
from social_reply.application.event_ingestion.xchat_recovery import sweep_xchat_recovery
from social_reply.application.event_ingestion.xchat_subscription import (
    ensure_xchat_subscriptions,
)
from social_reply.application.message_delivery.sweep import sweep_outbox
from social_reply.application.reply_decision.jobs import sweep_decision_jobs
from social_reply.infrastructure.queue.actor_loop import submit_on_actor_loop
from social_reply.shared.config import Settings, get_settings

Lane = Literal["core", "inspection"]
Sweep = Callable[[], Coroutine[Any, Any, list[Any]]]

logger = logging.getLogger(__name__)
_SHUTDOWN_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class SweepSpec:
    name: str
    lane: Lane
    interval_seconds: float
    warn_after_seconds: float
    run: Sweep


@dataclass
class SweepRuntime:
    running: bool
    next_due: float
    started_at: float | None
    warned: bool
    future: Future[Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _build_sweep_specs(settings: Settings) -> tuple[SweepSpec, ...]:
    core_interval = settings.scheduler_core_interval_seconds
    core_warn_after = settings.scheduler_core_warn_after_seconds
    inspection_warn_after = settings.scheduler_inspection_warn_after_seconds
    specs: list[SweepSpec] = [
        SweepSpec(
            "sweep_provisioning_jobs",
            "core",
            core_interval,
            core_warn_after,
            sweep_provisioning_jobs,
        ),
        SweepSpec(
            "sweep_initial_raw_events",
            "core",
            core_interval,
            core_warn_after,
            sweep_initial_raw_events,
        ),
        SweepSpec(
            "sweep_decision_jobs",
            "core",
            core_interval,
            core_warn_after,
            sweep_decision_jobs,
        ),
        SweepSpec(
            "sweep_outbox",
            "core",
            core_interval,
            core_warn_after,
            sweep_outbox,
        ),
    ]
    if settings.xchat_enabled:
        specs.append(
            SweepSpec(
                "sweep_xchat_recovery",
                "core",
                settings.xchat_recovery_sweep_interval_seconds,
                core_warn_after,
                sweep_xchat_recovery,
            )
        )
    if settings.chatwoot_enabled:
        from social_reply.application.event_ingestion.reconcile import (
            reconcile_chatwoot_messages,
        )

        specs.append(
            SweepSpec(
                "reconcile_chatwoot_messages",
                "inspection",
                settings.chatwoot_reconcile_interval_seconds,
                inspection_warn_after,
                reconcile_chatwoot_messages,
            )
        )
    if settings.x_legacy_dm_enabled:
        specs.append(
            SweepSpec(
                "poll_x_direct_messages",
                "inspection",
                settings.x_dm_poll_interval_seconds,
                inspection_warn_after,
                poll_x_direct_messages,
            )
        )
    if settings.xchat_enabled:
        specs.append(
            SweepSpec(
                "poll_xchat_messages",
                "inspection",
                settings.xchat_poll_interval_seconds,
                inspection_warn_after,
                poll_xchat_messages,
            )
        )
    if settings.x_activity_enabled:
        specs.extend(
            (
                SweepSpec(
                    "ensure_xchat_subscriptions",
                    "inspection",
                    settings.xchat_subscription_check_interval_seconds,
                    inspection_warn_after,
                    ensure_xchat_subscriptions,
                ),
                SweepSpec(
                    "ensure_x_webhooks_valid",
                    "inspection",
                    settings.x_webhook_check_interval_seconds,
                    inspection_warn_after,
                    ensure_x_webhooks_valid,
                ),
            )
        )
    if settings.facebook_messenger_enabled or settings.instagram_messaging_enabled:
        specs.append(
            SweepSpec(
                "reconcile_meta_account_health",
                "inspection",
                settings.meta_health_check_interval_seconds,
                inspection_warn_after,
                reconcile_meta_account_health,
            )
        )
    if settings.feishu_enabled:
        specs.append(
            SweepSpec(
                "reconcile_feishu_account_health",
                "inspection",
                settings.feishu_health_check_interval_seconds,
                inspection_warn_after,
                reconcile_feishu_account_health,
            )
        )
    return tuple(specs)


def _new_runtimes(specs: tuple[SweepSpec, ...], *, now: float) -> dict[str, SweepRuntime]:
    return {
        spec.name: SweepRuntime(
            running=False,
            next_due=now,
            started_at=None,
            warned=False,
        )
        for spec in specs
    }


async def _run_sweep(
    spec: SweepSpec,
    runtime: SweepRuntime,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    started_at = clock()
    with runtime.lock:
        runtime.running = True
        runtime.started_at = started_at
        runtime.warned = False
    recovered_count = 0
    status = "success"
    try:
        recovered = await spec.run()
        recovered_count = len(recovered)
    except Exception:  # noqa: BLE001 - each sweep is an independent recovery boundary
        status = "failure"
        logger.exception(
            "scheduler sweep completed",
            extra={
                "sweep_name": spec.name,
                "lane": spec.lane,
                "duration": clock() - started_at,
                "recovered_count": recovered_count,
                "status": status,
            },
        )
    else:
        logger.info(
            "scheduler sweep completed",
            extra={
                "sweep_name": spec.name,
                "lane": spec.lane,
                "duration": clock() - started_at,
                "recovered_count": recovered_count,
                "status": status,
            },
        )
    finally:
        with runtime.lock:
            runtime.running = False
            runtime.started_at = None
            runtime.warned = False


def _observe_future(
    spec: SweepSpec,
    runtime: SweepRuntime,
    completed: Future[Any],
) -> None:
    try:
        completed.result()
    except BaseException as exc:  # noqa: BLE001 - the callback must observe all failures
        if completed.cancelled():
            logger.warning(
                "scheduler sweep future cancelled",
                extra={
                    "sweep_name": spec.name,
                    "lane": spec.lane,
                    "status": "future_cancelled",
                },
            )
        else:
            logger.error(
                "scheduler sweep future failed",
                exc_info=(type(exc), exc, exc.__traceback__),
                extra={
                    "sweep_name": spec.name,
                    "lane": spec.lane,
                    "status": "future_failure",
                },
            )
    finally:
        with runtime.lock:
            if runtime.future is completed:
                runtime.future = None


def _tick(
    specs: tuple[SweepSpec, ...],
    runtimes: dict[str, SweepRuntime],
    *,
    now: float | None = None,
    submit: Callable[[Coroutine[Any, Any, None]], Future[Any]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    submit_sweep = submit_on_actor_loop if submit is None else submit
    current = clock() if now is None else now
    for spec in specs:
        runtime = runtimes[spec.name]
        warning_duration: float | None = None
        with runtime.lock:
            if (
                runtime.running
                and runtime.started_at is not None
                and not runtime.warned
                and current - runtime.started_at >= spec.warn_after_seconds
            ):
                runtime.warned = True
                warning_duration = current - runtime.started_at
        if warning_duration is not None:
            logger.warning(
                "scheduler sweep exceeded warning threshold",
                extra={
                    "sweep_name": spec.name,
                    "lane": spec.lane,
                    "duration": warning_duration,
                    "warn_after_seconds": spec.warn_after_seconds,
                    "max_instances": 1,
                },
            )
        if current < runtime.next_due:
            continue

        runtime.next_due = current + spec.interval_seconds
        with runtime.lock:
            in_flight = runtime.running or runtime.future is not None
        if in_flight:
            continue

        coroutine = _run_sweep(spec, runtime, clock=clock)
        try:
            future = submit_sweep(coroutine)
        except Exception:  # noqa: BLE001 - one submission must not block the remaining specs
            coroutine.close()
            logger.exception(
                "scheduler sweep submission failed",
                extra={
                    "sweep_name": spec.name,
                    "lane": spec.lane,
                    "status": "submission_failure",
                },
            )
            continue
        with runtime.lock:
            runtime.future = future
        future.add_done_callback(
            lambda done, target=runtime, sweep=spec: _observe_future(sweep, target, done)
        )


def _drain_running_sweeps(
    runtimes: dict[str, SweepRuntime],
    *,
    timeout_seconds: float = _SHUTDOWN_GRACE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = clock() + timeout_seconds
    while True:
        runtime_snapshots: list[tuple[str, Future[Any] | None, bool]] = []
        for name, runtime in runtimes.items():
            with runtime.lock:
                runtime_snapshots.append((name, runtime.future, runtime.running))
        running = [
            name
            for name, future, is_running in runtime_snapshots
            if (future is not None and not future.done()) or is_running
        ]
        if not running:
            return
        remaining = deadline - clock()
        if remaining <= 0:
            logger.warning(
                "scheduler shutdown grace period expired",
                extra={"running_sweeps": running, "status": "still_running"},
            )
            return
        sleep(min(0.05, remaining))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    specs = _build_sweep_specs(settings)
    stop = threading.Event()

    def request_shutdown(signum: int, _frame: object) -> None:
        logger.info("scheduler shutdown requested", extra={"signal": signum})
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, request_shutdown)

    runtimes = _new_runtimes(specs, now=time.monotonic())
    try:
        while not stop.is_set():
            _tick(specs, runtimes)
            stop.wait(settings.scheduler_tick_seconds)
    except KeyboardInterrupt:
        logger.info("scheduler shutdown requested", extra={"signal": "KeyboardInterrupt"})
    finally:
        _drain_running_sweeps(runtimes)
        logger.info("scheduler stopped")


if __name__ == "__main__":
    main()
