"""Recovery scheduler: uv run python -m apps.scheduler.main."""

import logging
import time
from collections.abc import Awaitable, Callable

from social_reply.application.account_management.jobs import sweep_provisioning_jobs
from social_reply.application.event_ingestion.reconcile import reconcile_chatwoot_messages
from social_reply.application.event_ingestion.x_dm_poll import poll_x_direct_messages
from social_reply.application.message_delivery.sweep import sweep_outbox
from social_reply.application.reply_decision.jobs import sweep_decision_jobs
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop

_INTERVAL_SECONDS = 3
_SWEEPS: tuple[tuple[str, Callable[[], Awaitable[list]]], ...] = (
    ("reconcile_chatwoot_messages", reconcile_chatwoot_messages),
    ("sweep_provisioning_jobs", sweep_provisioning_jobs),
    ("sweep_decision_jobs", sweep_decision_jobs),
    ("sweep_outbox", sweep_outbox),
    # X DM webhook 投递不可靠，改用轮询拉取新 DM
    ("poll_x_direct_messages", poll_x_direct_messages),
)

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    while True:
        for name, sweep in _SWEEPS:
            try:
                recovered = run_on_actor_loop(sweep())
                if recovered:
                    logger.info("%s: recovered %d items", name, len(recovered))
            except Exception:  # noqa: BLE001 - recovery tasks must remain isolated
                logger.exception("%s failed; retrying in %ds", name, _INTERVAL_SECONDS)
        time.sleep(_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
