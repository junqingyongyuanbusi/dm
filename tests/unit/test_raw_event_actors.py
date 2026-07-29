import inspect

from social_reply.application.event_ingestion.actors import (
    process_chatwoot_event,
    process_initial_chatwoot_event_actor,
)
from social_reply.application.event_ingestion.direct_actors import (
    process_direct_event,
    process_initial_direct_event_actor,
)


def test_initial_raw_event_actors_use_a_versioned_dedicated_queue() -> None:
    assert process_direct_event.queue_name == "default"
    assert process_chatwoot_event.queue_name == "default"
    assert process_initial_direct_event_actor.actor_name == "process_initial_direct_event_v1"
    assert process_initial_chatwoot_event_actor.actor_name == "process_initial_chatwoot_event_v1"
    assert process_initial_direct_event_actor.queue_name == "initial_raw_v1"
    assert process_initial_chatwoot_event_actor.queue_name == "initial_raw_v1"
    assert tuple(inspect.signature(process_direct_event.fn).parameters) == (
        "raw_event_id",
        "events",
    )
    assert tuple(inspect.signature(process_chatwoot_event.fn).parameters) == ("raw_event_id",)
