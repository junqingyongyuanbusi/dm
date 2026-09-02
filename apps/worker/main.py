"""Dramatiq worker 入口：uv run dramatiq apps.worker.main"""

import social_reply.application.account_management.actors  # noqa: F401  注册 actor
import social_reply.application.event_ingestion.direct_actors  # noqa: F401  注册 actor
import social_reply.application.event_ingestion.xchat_actors  # noqa: F401  注册 actor
import social_reply.application.handoff_notifications.actors  # noqa: F401  注册 actor
import social_reply.application.message_delivery.actors  # noqa: F401  注册 actor
import social_reply.application.reply_decision.actors  # noqa: F401  注册 actor
from social_reply.shared.logging import configure_safe_http_client_logging

configure_safe_http_client_logging()
