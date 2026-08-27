import asyncio
import uuid

import pytest

from social_reply.application.reply_decision.business_prompt import (
    acquire_business_prompt_xact_lock,
    business_prompt_provenance_is_current,
)
from social_reply.domain.reply.business_prompt import BusinessPromptInstructions
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


async def test_prompt_provenance_readers_share_scope_lock_while_save_waits(session) -> None:
    instructions = BusinessPromptInstructions("Keep replies concise and factual.")
    version_id = uuid.uuid4()
    session.add(
        models.ReplyBusinessPromptVersion(
            id=version_id,
            tenant_id="default",
            brand_id="brand-locking",
            revision=1,
            content=instructions.text,
            content_hash=instructions.content_hash,
            created_by="test",
        )
    )
    await session.flush()
    session.add(
        models.ReplyBusinessPrompt(
            tenant_id="default",
            brand_id="brand-locking",
            active_version_id=version_id,
            revision=1,
            content_hash=instructions.content_hash,
            updated_by="test",
        )
    )
    await session.commit()

    session_factory = get_session_factory()
    async with (
        session_factory() as first_reader,
        session_factory() as second_reader,
        session_factory() as writer,
    ):
        assert await business_prompt_provenance_is_current(
            first_reader,
            tenant_id="default",
            brand_id="brand-locking",
            version_id=version_id,
            content_hash=instructions.content_hash,
        )
        assert await asyncio.wait_for(
            business_prompt_provenance_is_current(
                second_reader,
                tenant_id="default",
                brand_id="brand-locking",
                version_id=version_id,
                content_hash=instructions.content_hash,
            ),
            timeout=1,
        )

        writer_acquired = asyncio.Event()

        async def acquire_writer_lock() -> None:
            await acquire_business_prompt_xact_lock(
                writer,
                "default",
                "brand-locking",
            )
            writer_acquired.set()

        writer_task = asyncio.create_task(acquire_writer_lock())
        await asyncio.sleep(0.05)
        assert not writer_acquired.is_set()

        await first_reader.rollback()
        await second_reader.rollback()
        await asyncio.wait_for(writer_acquired.wait(), timeout=1)
        await writer.rollback()
        await writer_task
