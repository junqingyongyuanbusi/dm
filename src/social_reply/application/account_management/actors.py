import dramatiq

import social_reply.infrastructure.queue.broker  # noqa: F401
from social_reply.application.account_management.jobs import process_provisioning_job
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop


@dramatiq.actor(max_retries=0)
def process_platform_provisioning(job_id: str) -> None:
    run_on_actor_loop(process_provisioning_job(job_id))
