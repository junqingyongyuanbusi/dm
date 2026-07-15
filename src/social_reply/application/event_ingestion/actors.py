import dramatiq

import social_reply.infrastructure.queue.broker  # noqa: F401  确保 broker 先初始化
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop


@dramatiq.actor(max_retries=3)
def process_chatwoot_event(raw_event_id: str) -> None:
    from social_reply.application.event_ingestion.processor import process_raw_event

    run_on_actor_loop(process_raw_event(raw_event_id))
