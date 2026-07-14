"""Dramatiq worker 入口：uv run dramatiq apps.worker.main"""

import social_reply.application.event_ingestion.actors  # noqa: F401  注册 actor
