import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Select, Update
from sqlalchemy.dialects import postgresql

from social_reply.application.message_delivery import sweep as sweep_module


def _capture_sweep(monkeypatch, pending_batches=((),), *, stale_candidate_ids=()):
    statements = []
    pending_results = iter(pending_batches)
    session = AsyncMock()
    session.__aenter__.return_value = session
    stale_candidates = MagicMock()
    stale_candidates.all.return_value = stale_candidate_ids
    session.scalars.return_value = stale_candidates

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
    stale_candidate_ids = tuple(uuid.UUID(int=1000 + index) for index in range(100))
    session, statements, dispatch = _capture_sweep(
        monkeypatch, stale_candidate_ids=stale_candidate_ids
    )

    assert await sweep_module.sweep_outbox() == []

    session.scalars.assert_awaited_once()
    session.scalars.return_value.all.assert_called_once_with()
    candidate_statement = session.scalars.await_args.args[0]
    candidate_sql = _compile_sql(candidate_statement)
    assert candidate_sql.startswith("SELECT outbox_messages.id FROM outbox_messages WHERE")
    assert candidate_sql.count("outbox_messages.status = 'SENDING'") == 1
    assert candidate_sql.count("outbox_messages.locked_at <") == 1
    assert candidate_sql.endswith(
        "ORDER BY outbox_messages.locked_at, outbox_messages.id LIMIT 100 FOR UPDATE SKIP LOCKED"
    )
    assert " OFFSET " not in candidate_sql

    stale_statement = next(
        statement
        for statement in statements
        if isinstance(statement, Update) and "RETURNING" in _compile_sql(statement)
    )
    stale_sql = _compile_sql(stale_statement)
    candidate_id_literals = ", ".join(f"'{candidate_id}'" for candidate_id in stale_candidate_ids)
    assert f"outbox_messages.id IN ({candidate_id_literals})" in stale_sql
    assert "SELECT" not in stale_sql
    # The fixed locked batch and the UPDATE share the same staleness guard.
    assert stale_sql.count("outbox_messages.status = 'SENDING'") == 1
    assert stale_sql.count("outbox_messages.locked_at <") == 1
    assert stale_statement.compile().params["locked_at_1"] == (
        candidate_statement.compile().params["locked_at_1"]
    )
    assert "status='NEEDS_REVIEW'" in stale_sql
    assert "last_error_code='STALE_SENDING'" in stale_sql
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
