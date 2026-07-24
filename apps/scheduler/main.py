"""Recovery scheduler: uv run python -m apps.scheduler.main."""

import logging
import time
from collections.abc import Awaitable, Callable

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
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop
from social_reply.shared.config import Settings, get_settings

_INTERVAL_SECONDS = 3


def _build_sweeps(settings: Settings) -> tuple[tuple[str, Callable[[], Awaitable[list]]], ...]:
    sweeps: list[tuple[str, Callable[[], Awaitable[list]]]] = []
    if settings.chatwoot_enabled:
        from social_reply.application.event_ingestion.reconcile import (
            reconcile_chatwoot_messages,
        )

        sweeps.append(("reconcile_chatwoot_messages", reconcile_chatwoot_messages))
    sweeps.extend(
        (
            ("sweep_provisioning_jobs", sweep_provisioning_jobs),
            ("sweep_initial_raw_events", sweep_initial_raw_events),
            ("sweep_decision_jobs", sweep_decision_jobs),
        )
    )
    if settings.x_legacy_dm_enabled:
        sweeps.append(("poll_x_direct_messages", poll_x_direct_messages))
    if settings.xchat_enabled:
        sweeps.append(("poll_xchat_messages", poll_xchat_messages))
    if settings.x_activity_enabled:
        sweeps.append(("ensure_xchat_subscriptions", ensure_xchat_subscriptions))
    if settings.xchat_enabled:
        sweeps.append(("sweep_xchat_recovery", sweep_xchat_recovery))
    if settings.x_activity_enabled:
        sweeps.append(("ensure_x_webhooks_valid", ensure_x_webhooks_valid))
    if settings.facebook_messenger_enabled or settings.instagram_messaging_enabled:
        sweeps.append(("reconcile_meta_account_health", reconcile_meta_account_health))
    # Capability reconciliation must run before paused Outbox rows are released.
    sweeps.append(("sweep_outbox", sweep_outbox))
    return tuple(sweeps)


_SWEEPS = _build_sweeps(get_settings())

logger = logging.getLogger(__name__)


def _run_sweep_cycle(
    sweeps: tuple[tuple[str, Callable[[], Awaitable[list]]], ...] = _SWEEPS,
) -> None:
    for name, sweep in sweeps:
        try:
            recovered = run_on_actor_loop(sweep())
            if recovered:
                logger.info("%s: recovered %d items", name, len(recovered))
        except Exception:  # noqa: BLE001 - recovery tasks must remain isolated
            logger.exception("%s failed; retrying in %ds", name, _INTERVAL_SECONDS)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    get_settings()  # Fail startup if encryption or production safety settings are invalid.
    while True:
        _run_sweep_cycle()
        time.sleep(_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
