import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Select, Update
from sqlalchemy.dialects import postgresql

from social_reply.application.message_delivery import sweep as sweep_module


@pytest.fixture(autouse=True)
def reset_dispatch_cursor(monkeypatch):
    monkeypatch.setattr(sweep_module, "_dispatch_cursor", None)


def _capture_sweep(monkeypatch, pending_batches=((),)):
    statements = []
    pending_results = iter(pending_batches)
    session = AsyncMock()
    session.__aenter__.return_value = session

    async def execute(statement):
        statements.append(statement)
        result = MagicMock()
        if isinstance(statement, Select):
            result.scalars.return_value.all.return_value = next(pending_results)
        return result

    session.execute.side_effect = execute
    monkeypatch.setattr(sweep_module, "get_session_factory", lambda: lambda: session)
    dispatch = AsyncMock()
    monkeypatch.setattr(sweep_module, "dispatch_actor", dispatch)
    return session, statements, dispatch


def _compile_sql(statement):
    return " ".join(
        str(
            statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        ).split()
    )


async def test_sweep_limits_stale_update_and_eligible_ids_in_postgres(monkeypatch):
    session, statements, dispatch = _capture_sweep(monkeypatch)

    assert await sweep_module.sweep_outbox() == []

    stale_statement = next(
        statement
        for statement in statements
        if isinstance(statement, Update) and "RETURNING" in _compile_sql(statement)
    )
    stale_sql = _compile_sql(stale_statement)
    assert "outbox_messages.id IN (SELECT outbox_messages.id" in stale_sql
    assert (
        "ORDER BY outbox_messages.locked_at, outbox_messages.id LIMIT 100 FOR UPDATE" in stale_sql
    )
    assert "FOR UPDATE SKIP LOCKED" in stale_sql
    # Both candidate selection and the UPDATE itself must still require staleness.
    assert stale_sql.count("outbox_messages.status = 'SENDING'") == 2
    assert stale_sql.count("outbox_messages.locked_at <") == 2
    assert "RETURNING outbox_messages.id, outbox_messages.attempt_count" in stale_sql

    eligible_statements = [statement for statement in statements if isinstance(statement, Select)]
    assert len(eligible_statements) == 1
    eligible_sql = _compile_sql(eligible_statements[0])
    assert eligible_sql.startswith("SELECT outbox_messages.id FROM outbox_messages WHERE")
    assert "outbox_messages.status = 'PENDING'" in eligible_sql
    assert "outbox_messages.status = 'FAILED'" in eligible_sql
    assert "outbox_messages.next_attempt_at <=" in eligible_sql
    assert eligible_sql.endswith("ORDER BY outbox_messages.id LIMIT 100")
    assert " OFFSET " not in eligible_sql
    dispatch.assert_not_awaited()
    session.commit.assert_awaited_once()


async def test_empty_keyset_tail_wraps_with_a_bounded_query_in_the_same_sweep(monkeypatch):
    previous_id = uuid.UUID(int=500)
    wrapped_id = uuid.UUID(int=1)
    monkeypatch.setattr(sweep_module, "_dispatch_cursor", previous_id)
    session, statements, dispatch = _capture_sweep(monkeypatch, ((), (wrapped_id,)))

    assert await sweep_module.sweep_outbox() == [wrapped_id]

    eligible_sql = [
        _compile_sql(statement) for statement in statements if isinstance(statement, Select)
    ]
    assert len(eligible_sql) == 2
    assert f"outbox_messages.id > '{previous_id}'" in eligible_sql[0]
    assert "outbox_messages.id >" not in eligible_sql[1]
    assert all(query.endswith("ORDER BY outbox_messages.id LIMIT 100") for query in eligible_sql)
    assert sweep_module._dispatch_cursor == wrapped_id
    assert dispatch.await_args.args[1] == str(wrapped_id)
    session.commit.assert_awaited_once()


async def test_dispatch_cancellation_advances_only_through_the_attempted_id(monkeypatch):
    first_id, second_id = uuid.UUID(int=1), uuid.UUID(int=2)
    session, statements, dispatch = _capture_sweep(
        monkeypatch, ((first_id, second_id), (second_id,))
    )
    dispatch.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await sweep_module.sweep_outbox()

    assert sweep_module._dispatch_cursor == first_id
    assert dispatch.await_count == 1
    session.commit.assert_awaited_once()

    dispatch.side_effect = None
    assert await sweep_module.sweep_outbox() == [second_id]
    eligible_sql = [
        _compile_sql(statement) for statement in statements if isinstance(statement, Select)
    ]
    assert f"outbox_messages.id > '{first_id}'" in eligible_sql[-1]
    assert sweep_module._dispatch_cursor == second_id
