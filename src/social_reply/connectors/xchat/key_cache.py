import json

from sqlalchemy import select, update

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

_CONFIG_FIELD = "xchat_conversation_key_events"


def canonical_conversation_id(value: str) -> str:
    return str(value).replace("-", ":")


def conversation_key_events(config: dict, conversation_id: str) -> list[str]:
    cached = dict(config.get(_CONFIG_FIELD) or {})
    canonical_id = canonical_conversation_id(conversation_id)
    value = cached.get(canonical_id) or cached.get(conversation_id) or []
    return [str(item) for item in value if item]


async def save_conversation_key_events(
    account_id,
    conversation_id: str,
    events: list[str],
) -> None:
    conversation_id = canonical_conversation_id(conversation_id)
    values = list(dict.fromkeys(str(item) for item in events if item))
    if not values:
        return
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.PlatformAccount)
                .where(models.PlatformAccount.id == account_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
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
            .values(
                config=json.loads(json.dumps(config)),
                config_version=models.PlatformAccount.config_version + 1,
            )
        )
        await session.commit()
