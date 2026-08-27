"""Retire Prompt-derived unsent work before a legacy application rollback."""

import asyncio
import json

from social_reply.application.reply_decision.business_prompt_retirement import (
    retire_business_prompt_work_for_rollback,
)
from social_reply.infrastructure.database.engine import get_session_factory


async def _run() -> None:
    async with get_session_factory()() as session:
        report = await retire_business_prompt_work_for_rollback(session)
        await session.commit()
    print(
        json.dumps(
            {
                "status": "ok",
                "conversations": report.conversations,
                "outboxes_cancelled": report.outboxes_cancelled,
                "drafts_rejected": report.drafts_rejected,
            },
            sort_keys=True,
        )
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
