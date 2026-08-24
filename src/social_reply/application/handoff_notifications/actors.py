import dramatiq

import social_reply.infrastructure.queue.broker  # noqa: F401
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop


@dramatiq.actor(max_retries=0)
def deliver_handoff_notification_actor(intent_id: str) -> None:
    from social_reply.application.handoff_notifications.sender import (
        deliver_handoff_notification,
    )

    run_on_actor_loop(deliver_handoff_notification(intent_id))
