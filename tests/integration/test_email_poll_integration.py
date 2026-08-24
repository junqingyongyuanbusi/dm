import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from social_reply.application.event_ingestion import email_poll
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

pytestmark = pytest.mark.integration


def _message(
    message_id: str,
    *,
    subject: str = "Customer secret subject",
    body: str = "Customer secret body",
    headers: dict[str, str] | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = "Alice <alice@customer.test>"
    message["To"] = "support@example.com"
    message["Subject"] = subject
    message["Message-ID"] = f"<{message_id}>"
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(body)
    return bytes(message)


class _FakeClient:
    def __init__(
        self,
        *,
        uidvalidity: int,
        uids: tuple[int, ...],
        messages: dict[int, bytes] | None = None,
        sizes: dict[int, int] | None = None,
        fail_fetch_uid: int | None = None,
    ) -> None:
        self.uidvalidity = uidvalidity
        self.uids = uids
        self.messages = messages or {}
        self.sizes = sizes or {uid: len(raw) for uid, raw in self.messages.items()}
        self.fail_fetch_uid = fail_fetch_uid
        self.search_starts: list[int] = []
        self.size_fetches: list[int] = []
        self.fetches: list[int] = []
        self.closed = False

    async def connect(self) -> int:
        return self.uidvalidity

    async def search_uids(self, *, start_uid: int = 1) -> tuple[int, ...]:
        self.search_starts.append(start_uid)
        return tuple(uid for uid in self.uids if uid >= start_uid)

    async def fetch_message_size(self, uid: int) -> int:
        self.size_fetches.append(uid)
        return self.sizes[uid]

    async def fetch_message(self, uid: int) -> bytes:
        self.fetches.append(uid)
        if uid == self.fail_fetch_uid:
            raise RuntimeError("message processing failed")
        return self.messages[uid]

    async def aclose(self) -> None:
        self.closed = True


async def _seed_account(
    session,
    *,
    name: str = "support",
    config: dict | None = None,
    credentials: dict | None = None,
) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="email",
            name=name,
            external_account_id=f"{name}@example.com",
            public_id=f"{name}-{account_id}",
            credential_bundle=encrypt_secret_bundle(
                credentials or {"username": name, "password": "app-password"}
            ),
            config=config
            or {
                "imap_host": "imap.example.com",
                "imap_port": 993,
                "self_address": "support@example.com",
                "mailbox": "INBOX",
            },
            capability={"dm": True, "max_text_length": 4000},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()
    return account_id


async def _seed_checkpoint(
    session,
    account_id: uuid.UUID,
    *,
    uidvalidity: int,
    last_uid: int,
) -> None:
    await session.execute(
        insert(models.PlatformCheckpoint).values(
            tenant_id="tenant-a",
            platform_account_id=account_id,
            stream="EMAIL_IMAP",
            scope_key="",
            cursor=email_poll.EmailCursor(uidvalidity, last_uid).serialize(),
            bootstrapped=True,
        )
    )
    await session.commit()


async def _checkpoint(session, account_id: uuid.UUID) -> models.PlatformCheckpoint:
    return (
        await session.execute(
            select(models.PlatformCheckpoint)
            .where(
                models.PlatformCheckpoint.platform_account_id == account_id,
                models.PlatformCheckpoint.stream == "EMAIL_IMAP",
                models.PlatformCheckpoint.scope_key == "",
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _settings(*, max_messages: int = 100):
    return SimpleNamespace(
        email_enabled=True,
        email_poll_interval_seconds=60,
        email_max_messages_per_poll=max_messages,
        email_network_timeout_seconds=10.0,
        email_allowed_hosts=frozenset({"imap.example.com"}),
    )


def _factory_for(clients: dict[str, _FakeClient]):
    def factory(**kwargs):
        assert kwargs["password"] == "app-password"
        return clients[kwargs["username"]]

    return factory


async def _insert_normalized(account_id: uuid.UUID, event, raw_event_id: uuid.UUID) -> uuid.UUID:
    normalized_id = uuid.uuid4()
    async with email_poll.get_session_factory()() as session:
        await session.execute(
            insert(models.NormalizedEvent).values(
                id=normalized_id,
                tenant_id="tenant-a",
                platform="email",
                platform_account_id=account_id,
                external_event_id=event.external_event_id,
                event_type="dm.message.created",
                raw_event_id=raw_event_id,
                external_conversation_id=event.external_conversation_id,
                event_metadata={},
            )
        )
        await session.execute(
            update(models.RawEvent)
            .where(models.RawEvent.id == raw_event_id)
            .values(processing_status="PROCESSED")
        )
        await session.commit()
    return normalized_id


async def test_email_poll_bootstraps_to_current_max_uid_without_fetching_history(
    session, monkeypatch
):
    account_id = await _seed_account(session)
    client = _FakeClient(uidvalidity=42, uids=(2, 9, 12))
    monkeypatch.setattr(email_poll, "get_settings", _settings)

    assert (
        await email_poll.poll_email_messages(
            scheduler_owner="test",
            client_factory=_factory_for({"support": client}),
        )
        == []
    )

    checkpoint = await _checkpoint(session, account_id)
    assert email_poll.EmailCursor.parse(checkpoint.cursor) == email_poll.EmailCursor(42, 12)
    assert checkpoint.bootstrapped is True
    assert client.search_starts == [1]
    assert client.fetches == []
    assert client.closed is True
    assert (await session.execute(select(models.RawEvent))).first() is None


async def test_email_poll_sorts_uids_and_enforces_per_poll_limit(session, monkeypatch):
    account_id = await _seed_account(session)
    await _seed_checkpoint(session, account_id, uidvalidity=42, last_uid=3)
    client = _FakeClient(
        uidvalidity=42,
        uids=(7, 5, 6),
        messages={
            5: _message("message-5@example.com"),
            6: _message("message-6@example.com"),
            7: _message("message-7@example.com"),
        },
    )
    ingested: list[str] = []

    async def fake_ingest(event, *, raw_event_id):
        ingested.append(event.external_event_id)
        return await _insert_normalized(account_id, event, raw_event_id)

    monkeypatch.setattr(email_poll, "get_settings", lambda: _settings(max_messages=2))
    monkeypatch.setattr(email_poll, "ingest_canonical_event", fake_ingest)

    result = await email_poll.poll_email_messages(
        scheduler_owner="test",
        client_factory=_factory_for({"support": client}),
    )

    assert result == ["42:5", "42:6"]
    assert ingested == result
    assert client.fetches == [5, 6]
    checkpoint = await _checkpoint(session, account_id)
    assert email_poll.EmailCursor.parse(checkpoint.cursor).last_uid == 6


async def test_email_poll_uidvalidity_change_records_resolved_gap_and_reanchors(
    session, monkeypatch
):
    account_id = await _seed_account(session)
    await _seed_checkpoint(session, account_id, uidvalidity=41, last_uid=100)
    client = _FakeClient(uidvalidity=42, uids=(1, 8, 13))
    monkeypatch.setattr(email_poll, "get_settings", _settings)

    assert (
        await email_poll.poll_email_messages(
            scheduler_owner="test",
            client_factory=_factory_for({"support": client}),
        )
        == []
    )

    checkpoint = await _checkpoint(session, account_id)
    assert email_poll.EmailCursor.parse(checkpoint.cursor) == email_poll.EmailCursor(42, 13)
    assert client.fetches == []
    gap = (
        await session.execute(
            select(models.SyncGap).where(models.SyncGap.checkpoint_id == checkpoint.id)
        )
    ).scalar_one()
    assert gap.gap_type == "EMAIL_UIDVALIDITY_CHANGED"
    assert gap.status == "RESOLVED"
    assert gap.detail == {
        "previous_uidvalidity": 41,
        "current_uidvalidity": 42,
        "reanchored_last_uid": 13,
    }


async def test_email_poll_ignored_message_advances_and_raw_evidence_is_non_sensitive(
    session, monkeypatch
):
    account_id = await _seed_account(session)
    await _seed_checkpoint(session, account_id, uidvalidity=42, last_uid=4)
    raw = _message(
        "ignored@example.com",
        subject="Top secret subject",
        body="Top secret body",
        headers={"Auto-Submitted": "auto-replied"},
    )
    client = _FakeClient(uidvalidity=42, uids=(5,), messages={5: raw})
    monkeypatch.setattr(email_poll, "get_settings", _settings)

    assert (
        await email_poll.poll_email_messages(
            scheduler_owner="test",
            client_factory=_factory_for({"support": client}),
        )
        == []
    )

    checkpoint = await _checkpoint(session, account_id)
    assert email_poll.EmailCursor.parse(checkpoint.cursor).last_uid == 5
    raw_event = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw_event.processing_status == "IGNORED_AUTO_SUBMITTED"
    assert set(raw_event.payload) == {"uid", "uidvalidity", "sha256", "size"}
    assert raw_event.context == {}
    persisted = json.dumps(
        {"payload": raw_event.payload, "context": raw_event.context},
        ensure_ascii=False,
    )
    assert "Top secret" not in persisted
    assert "ignored@example.com" not in persisted
    assert bytes(raw).decode(errors="ignore") not in persisted


async def test_email_poll_oversized_message_is_ignored_before_body_download(session, monkeypatch):
    account_id = await _seed_account(session)
    await _seed_checkpoint(session, account_id, uidvalidity=42, last_uid=4)
    oversized = email_poll.MAX_INBOUND_MESSAGE_BYTES + 1
    client = _FakeClient(uidvalidity=42, uids=(5,), sizes={5: oversized})
    monkeypatch.setattr(email_poll, "get_settings", _settings)

    assert (
        await email_poll.poll_email_messages(
            scheduler_owner="test",
            client_factory=_factory_for({"support": client}),
        )
        == []
    )

    checkpoint = await _checkpoint(session, account_id)
    assert email_poll.EmailCursor.parse(checkpoint.cursor).last_uid == 5
    raw_event = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw_event.processing_status == "IGNORED_TOO_LARGE"
    assert raw_event.payload == {"uid": 5, "uidvalidity": 42, "size": oversized}
    assert raw_event.headers == {}
    assert raw_event.context == {}
    assert client.size_fetches == [5]
    assert client.fetches == []


async def test_email_poll_claim_transfer_during_normalization_fences_business_and_cursor(
    session, monkeypatch
):
    account_id = await _seed_account(session)
    await _seed_checkpoint(session, account_id, uidvalidity=42, last_uid=0)
    raw = _message("stale-owner@example.com")
    client = _FakeClient(uidvalidity=42, uids=(1,), messages={1: raw})
    real_to_thread = asyncio.to_thread
    transferred_claims = []
    ingest_calls = []

    async def transfer_during_normalize(func, *args, **kwargs):
        result = await real_to_thread(func, *args, **kwargs)
        if getattr(func, "__name__", "") == "normalize_message":
            async with email_poll.get_session_factory()() as transfer_session:
                checkpoint_id = await transfer_session.scalar(
                    select(models.PlatformCheckpoint.id).where(
                        models.PlatformCheckpoint.platform_account_id == account_id,
                        models.PlatformCheckpoint.stream == "EMAIL_IMAP",
                    )
                )
                await transfer_session.execute(
                    update(models.PlatformCheckpoint)
                    .where(models.PlatformCheckpoint.id == checkpoint_id)
                    .values(claim_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
                await transfer_session.commit()
            new_claim = await email_poll.claim_checkpoint(checkpoint_id, owner="new-owner")
            assert new_claim is not None
            transferred_claims.append(new_claim)
        return result

    async def unexpected_ingest(*args, **kwargs):
        ingest_calls.append((args, kwargs))
        raise AssertionError("stale owner must not write business data")

    monkeypatch.setattr(email_poll, "get_settings", _settings)
    monkeypatch.setattr(email_poll.asyncio, "to_thread", transfer_during_normalize)
    monkeypatch.setattr(email_poll, "ingest_canonical_event", unexpected_ingest)

    assert (
        await email_poll.poll_email_messages(
            scheduler_owner="stale-owner",
            client_factory=_factory_for({"support": client}),
        )
        == []
    )

    assert len(transferred_claims) == 1
    assert ingest_calls == []
    checkpoint = await _checkpoint(session, account_id)
    assert email_poll.EmailCursor.parse(checkpoint.cursor).last_uid == 0
    assert checkpoint.claim_token == transferred_claims[0].claim_token
    assert (await session.execute(select(models.NormalizedEvent))).first() is None
    assert (await session.execute(select(models.Message))).first() is None


async def test_email_poll_failure_does_not_advance_and_retry_reuses_raw_events(
    session, monkeypatch
):
    account_id = await _seed_account(session)
    await _seed_checkpoint(session, account_id, uidvalidity=42, last_uid=0)
    messages = {
        1: _message("message-1@example.com"),
        2: _message("message-2@example.com"),
    }
    first_client = _FakeClient(uidvalidity=42, uids=(1, 2), messages=messages)
    attempts: list[str] = []
    fail_second = True

    async def flaky_ingest(event, *, raw_event_id):
        nonlocal fail_second
        attempts.append(event.external_event_id)
        if event.external_event_id == "42:2" and fail_second:
            fail_second = False
            raise RuntimeError("processing failed")
        return await _insert_normalized(account_id, event, raw_event_id)

    monkeypatch.setattr(email_poll, "get_settings", _settings)
    monkeypatch.setattr(email_poll, "ingest_canonical_event", flaky_ingest)
    factory = _factory_for({"support": first_client})

    assert (
        await email_poll.poll_email_messages(
            scheduler_owner="test",
            client_factory=factory,
        )
        == []
    )
    checkpoint = await _checkpoint(session, account_id)
    assert email_poll.EmailCursor.parse(checkpoint.cursor).last_uid == 0
    raw_before = (await session.execute(select(models.RawEvent))).scalars().all()
    assert len(raw_before) == 2
    raw_ids_before = {row.external_event_id: row.id for row in raw_before}

    await session.execute(
        update(models.PlatformCheckpoint)
        .where(models.PlatformCheckpoint.id == checkpoint.id)
        .values(next_attempt_at=None)
    )
    await session.commit()
    second_client = _FakeClient(uidvalidity=42, uids=(1, 2), messages=messages)
    factory = _factory_for({"support": second_client})
    assert await email_poll.poll_email_messages(
        scheduler_owner="test-retry",
        client_factory=factory,
    ) == ["42:2"]

    checkpoint = await _checkpoint(session, account_id)
    assert email_poll.EmailCursor.parse(checkpoint.cursor).last_uid == 2
    raw_after = (await session.execute(select(models.RawEvent))).scalars().all()
    assert len(raw_after) == 2
    assert {row.external_event_id: row.id for row in raw_after} == raw_ids_before
    assert attempts == ["42:1", "42:2", "42:2"]
    normalized = (await session.execute(select(models.NormalizedEvent))).scalars().all()
    assert sorted(row.external_event_id for row in normalized) == ["42:1", "42:2"]


async def test_email_poll_same_message_id_does_not_suppress_distinct_imap_occurrence(
    session, monkeypatch
):
    account_id = await _seed_account(session)
    await _seed_checkpoint(session, account_id, uidvalidity=42, last_uid=9)
    await session.execute(
        insert(models.NormalizedEvent).values(
            tenant_id="tenant-a",
            platform="email",
            platform_account_id=account_id,
            external_event_id="42:9",
            event_type="dm.message.created",
            external_conversation_id="duplicate@example.com",
            event_metadata={},
        )
    )
    await session.commit()
    client = _FakeClient(
        uidvalidity=42,
        uids=(10,),
        messages={10: _message("duplicate@example.com")},
    )
    ingested: list[str] = []

    async def fake_ingest(event, *, raw_event_id):
        ingested.append(event.external_event_id)
        return await _insert_normalized(account_id, event, raw_event_id)

    monkeypatch.setattr(email_poll, "get_settings", _settings)
    monkeypatch.setattr(email_poll, "ingest_canonical_event", fake_ingest)

    assert await email_poll.poll_email_messages(
        scheduler_owner="test",
        client_factory=_factory_for({"support": client}),
    ) == ["42:10"]

    checkpoint = await _checkpoint(session, account_id)
    assert email_poll.EmailCursor.parse(checkpoint.cursor).last_uid == 10
    raw_event = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw_event.processing_status == "PROCESSED"
    assert ingested == ["42:10"]


async def test_email_poll_records_bounded_stable_network_error_code(session, monkeypatch):
    account_id = await _seed_account(session)

    class NetworkFailureClient:
        async def connect(self) -> int:
            raise email_poll.EmailNetworkError("resolver leaked secret " + "x" * 200)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(email_poll, "get_settings", _settings)

    assert (
        await email_poll.poll_email_messages(
            scheduler_owner="test",
            client_factory=lambda **_kwargs: NetworkFailureClient(),
        )
        == []
    )

    checkpoint = await _checkpoint(session, account_id)
    run = (
        await session.execute(
            select(models.SyncRun).where(models.SyncRun.checkpoint_id == checkpoint.id)
        )
    ).scalar_one()
    assert run.status == "FAILED"
    assert run.error_code == "EMAIL_NETWORK_FAILED"
    assert run.error_message == "EMAIL_NETWORK_FAILED"


async def test_email_poll_timeout_does_not_block_other_accounts(session, monkeypatch):
    slow_id = await _seed_account(session, name="slow")
    valid_id = await _seed_account(session, name="valid")

    class SlowClient:
        def __init__(self) -> None:
            self.closed = False

        async def connect(self) -> int:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def aclose(self) -> None:
            self.closed = True

    slow_client = SlowClient()
    valid_client = _FakeClient(uidvalidity=42, uids=(4,))
    real_list_accounts = email_poll.list_active_accounts_by_platform
    accounts = await real_list_accounts("email")
    accounts_by_name = {account.name: account for account in accounts}

    async def ordered_accounts(platform: str):
        assert platform == "email"
        return [accounts_by_name["slow"], accounts_by_name["valid"]]

    monkeypatch.setattr(email_poll, "get_settings", _settings)
    monkeypatch.setattr(email_poll, "list_active_accounts_by_platform", ordered_accounts)
    monkeypatch.setattr(email_poll, "_account_poll_budget_seconds", lambda **_kwargs: 0.5)

    assert (
        await email_poll.poll_email_messages(
            scheduler_owner="test",
            client_factory=_factory_for({"slow": slow_client, "valid": valid_client}),
        )
        == []
    )

    slow_checkpoint = await _checkpoint(session, slow_id)
    valid_checkpoint = await _checkpoint(session, valid_id)
    slow_run = (
        await session.execute(
            select(models.SyncRun).where(models.SyncRun.checkpoint_id == slow_checkpoint.id)
        )
    ).scalar_one()
    assert slow_run.status == "FAILED"
    assert slow_run.error_code == "EMAIL_POLL_TIMEOUT"
    assert slow_run.error_message == "EMAIL_POLL_TIMEOUT"
    assert slow_client.closed is True
    assert email_poll.EmailCursor.parse(valid_checkpoint.cursor) == email_poll.EmailCursor(42, 4)
    assert valid_client.closed is True


async def test_email_poll_account_failure_does_not_block_other_accounts(session, monkeypatch):
    invalid_id = await _seed_account(
        session,
        name="invalid",
        config={"imap_port": 993, "self_address": "invalid@example.com"},
    )
    valid_id = await _seed_account(session, name="valid")
    valid_client = _FakeClient(uidvalidity=42, uids=(4,))
    monkeypatch.setattr(email_poll, "get_settings", _settings)

    assert (
        await email_poll.poll_email_messages(
            scheduler_owner="test",
            client_factory=_factory_for({"valid": valid_client}),
        )
        == []
    )

    invalid_checkpoint = await _checkpoint(session, invalid_id)
    valid_checkpoint = await _checkpoint(session, valid_id)
    invalid_run = (
        await session.execute(
            select(models.SyncRun).where(models.SyncRun.checkpoint_id == invalid_checkpoint.id)
        )
    ).scalar_one()
    assert invalid_run.status == "FAILED"
    assert invalid_run.error_code == "EMAIL_IMAP_HOST_INVALID"
    assert invalid_run.error_message == "EMAIL_IMAP_HOST_INVALID"
    assert email_poll.EmailCursor.parse(valid_checkpoint.cursor) == email_poll.EmailCursor(42, 4)
    assert valid_client.closed is True
