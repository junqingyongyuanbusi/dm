import uuid

import dramatiq

import social_reply.infrastructure.queue.broker  # noqa: F401
from social_reply.application.event_ingestion.xchat_recovery import recover_xchat_account_state
from social_reply.application.event_ingestion.xchat_webhook import process_xchat_raw_event
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop


@dramatiq.actor(max_retries=3)
def process_xchat_event(raw_event_id: str, account_id: str) -> None:
    run_on_actor_loop(
        process_xchat_raw_event(
            uuid.UUID(raw_event_id),
            uuid.UUID(account_id),
        )
    )


@dramatiq.actor(max_retries=3)
def recover_xchat_account(account_id: str) -> None:
    run_on_actor_loop(recover_xchat_account_state(uuid.UUID(account_id)))
