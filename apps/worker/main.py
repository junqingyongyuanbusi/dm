"""Dramatiq worker 入口：uv run dramatiq apps.worker.main"""

import social_reply.application.account_management.actors  # noqa: F401  注册 actor

# Chatwoot 关闭时仍注册兼容 actor，只用于排空切换前已经入队的 RawEvent。
import social_reply.application.event_ingestion.actors  # noqa: F401  注册 actor
import social_reply.application.event_ingestion.direct_actors  # noqa: F401  注册 actor
import social_reply.application.event_ingestion.xchat_actors  # noqa: F401  注册 actor
import social_reply.application.message_delivery.actors  # noqa: F401  注册 actor
import social_reply.application.reply_decision.actors  # noqa: F401  注册 actor
