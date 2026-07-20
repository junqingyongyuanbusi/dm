import json

from sqlalchemy import update

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

_CONFIG_FIELD = "xchat_conversation_key_events"


def conversation_key_events(config: dict, conversation_id: str) -> list[str]:
    cached = dict(config.get(_CONFIG_FIELD) or {})
    value = cached.get(conversation_id) or []
    return [str(item) for item in value if item]


async def save_conversation_key_events(
    account_id,
    conversation_id: str,
    events: list[str],
) -> None:
    values = list(dict.fromkeys(str(item) for item in events if item))
    if not values:
        return
    async with get_session_factory()() as session:
        row = await session.get(models.PlatformAccount, account_id)
        if row is None:
            return
        config = dict(row.config or {})
        cached = dict(config.get(_CONFIG_FIELD) or {})
        if cached.get(conversation_id) == values:
            return
        cached[conversation_id] = values
        config[_CONFIG_FIELD] = cached
        await session.execute(
            update(models.PlatformAccount)
            .where(models.PlatformAccount.id == account_id)
            .values(config=json.loads(json.dumps(config)))
        )
        await session.commit()
