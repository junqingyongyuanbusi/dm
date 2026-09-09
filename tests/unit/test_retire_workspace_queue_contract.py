import argparse
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from apps.cli.retire_workspace_queue import (
    EXPECTED_SCHEMA,
    CutoverRequest,
    parse_before,
    run_cutover,
)


def test_schema_pin_matches_repository_head():
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == [EXPECTED_SCHEMA]


@pytest.mark.parametrize("value", ["2026-01-01", "2026-01-01T00:00:00", "bad-date"])
def test_before_requires_aware_timestamp(value):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_before(value)


def test_before_normalizes_aware_offset_to_utc():
    assert parse_before("2026-01-01T08:00:00+08:00") == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"tenant": "tenant-b"}, "default_tenant_required"),
        ({"before": datetime.now(UTC) + timedelta(days=1)}, "before_in_future"),
        ({"before": datetime(2026, 1, 1)}, "before_timezone_required"),
        ({"apply": True}, "processes_stopped_confirmation_required"),
    ],
)
async def test_invalid_request_never_accesses_database(changes, expected):
    request = CutoverRequest(
        **{
            "tenant": "default",
            "cutover_id": uuid.uuid4(),
            "before": datetime(2020, 1, 1, tzinfo=UTC),
            **changes,
        }
    )
    session = AsyncMock()
    with pytest.raises(ValueError, match=expected):
        await run_cutover(session, request)
    session.execute.assert_not_called()
    session.begin.assert_not_called()


@pytest.mark.parametrize("confirmed", [False, True])
async def test_startup_request_requires_stopped_processes_and_bound_namespace(confirmed):
    request = CutoverRequest(
        tenant="default", cutover_id=uuid.uuid4(), before=datetime(2020, 1, 1, tzinfo=UTC),
        confirm_processes_stopped=confirmed,
    )
    request = replace(
        request, startup_namespace=(
            "dramatiq-cutover-invalid" if confirmed else f"dramatiq-cutover-{request.cutover_id.hex}"
        ),
    )
    session = AsyncMock()
    expected = "startup_namespace_mismatch" if confirmed else "processes_stopped_confirmation_required"
    with pytest.raises(ValueError, match=expected):
        await run_cutover(session, request)
    session.begin.assert_not_called()


def test_admin_set_is_canonical_and_repeated_ids_are_not_extra_grants():
    first, second = uuid.uuid4(), uuid.uuid4()
    request = CutoverRequest(
        tenant="default", cutover_id=uuid.uuid4(), before=datetime(2020, 1, 1, tzinfo=UTC),
        restore_admin_ids=(second, first, second),
    )
    assert request.parameters()["restore_admin_ids"] == sorted([str(first), str(second)])
