import dramatiq

import social_reply.infrastructure.queue.broker  # noqa: F401  确保 broker 先初始化
from social_reply.infrastructure.queue.actor_loop import run_on_actor_loop


@dramatiq.actor(max_retries=0)
def process_reply_decision(job_id: str) -> None:
    from social_reply.application.reply_decision.jobs import process_decision_job

    run_on_actor_loop(process_decision_job(job_id))
