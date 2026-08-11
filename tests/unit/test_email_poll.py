import asyncio
import imaplib
import logging
import threading
import uuid
from types import SimpleNamespace

import pytest

from social_reply.application.event_ingestion import email_poll
from social_reply.application.event_ingestion.email_poll import (
    EmailCursor,
    EmailCursorError,
    EmailPollAccountError,
    _account_contract,
)
from social_reply.application.event_ingestion.poll_sync import ClaimedCheckpoint, LeaseLostError


async def test_email_poll_disabled_returns_without_loading_accounts(monkeypatch):
    async def unexpected_account_lookup(_platform: str):
        raise AssertionError(
            "disabled email polling must not access account or network infrastructure"
        )

    monkeypatch.setattr(email_poll, "get_settings", lambda: SimpleNamespace(email_enabled=False))
    monkeypatch.setattr(
        email_poll,
        "list_active_accounts_by_platform",
        unexpected_account_lookup,
    )

    assert await email_poll.poll_email_messages() == []


async def test_email_poll_hash_and_normalize_run_off_event_loop(monkeypatch):
    account_id = uuid.uuid4()
    account = SimpleNamespace(
        id=account_id,
        tenant_id="tenant-a",
        config={
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "self_address": "support@example.com",
        },
        credential_bundle={"username": "support", "password": "password"},
    )
    claim = ClaimedCheckpoint(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        claim_token=uuid.uuid4(),
        revision=1,
        cursor=EmailCursor(42, 0).serialize(),
        bootstrapped=True,
        mode="POLL",
        active_gap=None,
    )
    raw = b"From: alice@example.com\r\n\r\nhello"
    worker_threads = []

    class Client:
        async def connect(self) -> int:
            return 42

        async def search_uids(self, *, start_uid: int = 1) -> tuple[int, ...]:
            assert start_uid == 1
            return (1,)

        async def fetch_message_size(self, uid: int) -> int:
            assert uid == 1
            return len(raw)

        async def fetch_message(self, uid: int) -> bytes:
            assert uid == 1
            return raw

        async def aclose(self) -> None:
            return None

    def fake_hash(value: bytes) -> str:
        assert value == raw
        worker_threads.append(("hash", threading.get_ident()))
        return "a" * 64

    def fake_normalize(self, value: bytes, *, uid: int, uidvalidity: int):
        del self
        assert value == raw
        assert (uid, uidvalidity) == (1, 42)
        worker_threads.append(("normalize", threading.get_ident()))
        return [], "IGNORED_TEST"

    async def no_op_claim(_claim):
        return None

    async def fake_reserve(**kwargs):
        assert kwargs["sha256"] == "a" * 64
        return email_poll._RawReservation(id=uuid.uuid4(), already_normalized=False)

    async def fake_mark(*_args, **_kwargs):
        return None

    async def fake_complete(*_args, **_kwargs):
        return None

    monkeypatch.setattr(email_poll, "_sha256_hex", fake_hash)
    monkeypatch.setattr(email_poll.EmailInboundAdapter, "normalize_message", fake_normalize)
    monkeypatch.setattr(email_poll, "require_claim", no_op_claim)
    monkeypatch.setattr(email_poll, "_reserve_raw_event", fake_reserve)
    monkeypatch.setattr(email_poll, "_mark_raw_event", fake_mark)
    monkeypatch.setattr(email_poll, "_complete_checkpoint_or_raise", fake_complete)
    event_loop_thread = threading.get_ident()

    assert (
        await email_poll._poll_account(
            account,
            claim=claim,
            poll_interval_seconds=60,
            max_messages=10,
            network_timeout_seconds=10,
            allowed_hosts=frozenset({"imap.example.com"}),
            client_factory=lambda **_kwargs: Client(),
        )
        == []
    )
    assert [name for name, _thread_id in worker_threads] == ["hash", "normalize"]
    assert all(thread_id != event_loop_thread for _name, thread_id in worker_threads)


async def test_email_poll_uses_bounded_concurrency_without_slow_account_head_of_line_blocking(
    monkeypatch,
):
    slow = SimpleNamespace(id="slow", tenant_id="tenant")
    healthy = SimpleNamespace(id="healthy", tenant_id="tenant")
    healthy_completed = asyncio.Event()

    async def accounts(_platform: str):
        return [slow, healthy]

    async def checkpoint(**_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def claim(*_args, **_kwargs):
        return SimpleNamespace()

    async def poll(account, **_kwargs):
        if account is slow:
            await asyncio.Event().wait()
        healthy_completed.set()
        return ["healthy-event"]

    async def fail(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        email_poll,
        "get_settings",
        lambda: SimpleNamespace(
            email_enabled=True,
            email_poll_interval_seconds=60,
            email_max_messages_per_poll=1,
            email_network_timeout_seconds=1,
            email_allowed_hosts=frozenset({"imap.example.com"}),
        ),
    )
    monkeypatch.setattr(email_poll, "list_active_accounts_by_platform", accounts)
    monkeypatch.setattr(email_poll, "ensure_checkpoint", checkpoint)
    monkeypatch.setattr(email_poll, "claim_checkpoint", claim)
    monkeypatch.setattr(email_poll, "_poll_account", poll)
    monkeypatch.setattr(email_poll, "fail_run", fail)
    monkeypatch.setattr(email_poll, "_account_poll_budget_seconds", lambda **_kwargs: 0.05)

    poll_task = asyncio.create_task(email_poll.poll_email_messages())
    async with asyncio.timeout(0.02):
        await healthy_completed.wait()
    assert not poll_task.done()
    assert await poll_task == ["healthy-event"]


async def test_email_poll_results_follow_account_order_not_completion_order(monkeypatch):
    first = SimpleNamespace(id="first", tenant_id="tenant")
    second = SimpleNamespace(id="second", tenant_id="tenant")
    release_first = asyncio.Event()

    async def poll(account, **_kwargs):
        if account is first:
            await release_first.wait()
            return ["first-event"]
        release_first.set()
        return ["second-event"]

    monkeypatch.setattr(
        email_poll,
        "get_settings",
        lambda: SimpleNamespace(
            email_enabled=True,
            email_poll_interval_seconds=60,
            email_max_messages_per_poll=1,
            email_network_timeout_seconds=1,
            email_allowed_hosts=frozenset({"imap.example.com"}),
        ),
    )
    monkeypatch.setattr(
        email_poll,
        "list_active_accounts_by_platform",
        lambda _platform: _async_result([first, second]),
    )
    monkeypatch.setattr(
        email_poll,
        "ensure_checkpoint",
        lambda **_kwargs: _async_result(SimpleNamespace(id=uuid.uuid4())),
    )
    monkeypatch.setattr(
        email_poll,
        "claim_checkpoint",
        lambda *_args, **_kwargs: _async_result(SimpleNamespace()),
    )
    monkeypatch.setattr(email_poll, "_poll_account", poll)

    assert await email_poll.poll_email_messages() == ["first-event", "second-event"]


async def _async_result(value):
    return value


async def test_email_poll_aclose_transport_error_does_not_replace_completed_result(monkeypatch):
    account = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        config={
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "self_address": "support@example.com",
        },
        credential_bundle={"username": "support", "password": "password"},
    )
    claim = ClaimedCheckpoint(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        claim_token=uuid.uuid4(),
        revision=1,
        cursor=None,
        bootstrapped=False,
        mode="POLL",
        active_gap=None,
    )

    class Client:
        async def connect(self) -> int:
            return 42

        async def search_uids(self, *, start_uid: int = 1) -> tuple[int, ...]:
            return ()

        async def aclose(self) -> None:
            raise OSError("connection already closed")

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(email_poll, "require_claim", no_op)
    monkeypatch.setattr(email_poll, "_complete_checkpoint_or_raise", no_op)

    assert (
        await email_poll._poll_account(
            account,
            claim=claim,
            poll_interval_seconds=60,
            max_messages=10,
            network_timeout_seconds=10,
            allowed_hosts=frozenset({"imap.example.com"}),
            client_factory=lambda **_kwargs: Client(),
        )
        == []
    )


async def test_email_poll_broad_exception_logs_traceback_and_persists_only_stable_code(
    monkeypatch, caplog
):
    account = SimpleNamespace(id=uuid.uuid4(), tenant_id="tenant-a")
    claim = SimpleNamespace()
    failures = []

    async def checkpoint(**_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def claim_checkpoint(*_args, **_kwargs):
        return claim

    sensitive_values = (
        "sensitive mailbox detail",
        "SQL parameters: customer_id=secret-customer",
        "customer text: do not log me",
        "reply_target=private@example.com",
    )

    async def broken_poll(*_args, **_kwargs):
        raise RuntimeError(" | ".join(sensitive_values))

    async def fail_run(*_args, **kwargs):
        failures.append(kwargs)

    monkeypatch.setattr(email_poll, "ensure_checkpoint", checkpoint)
    monkeypatch.setattr(email_poll, "claim_checkpoint", claim_checkpoint)
    monkeypatch.setattr(email_poll, "_poll_account", broken_poll)
    monkeypatch.setattr(email_poll, "fail_run", fail_run)

    with caplog.at_level(logging.ERROR):
        assert (
            await email_poll._poll_one_account(
                account,
                owner="owner",
                poll_interval_seconds=60,
                max_messages=1,
                network_timeout_seconds=1,
                allowed_hosts=frozenset({"imap.example.com"}),
                client_factory=lambda **_kwargs: None,
            )
            == []
        )

    assert failures == [
        {
            "error_code": "EMAIL_POLL_FAILED",
            "error_message": "EMAIL_POLL_FAILED",
            "retry_after_seconds": 60,
        }
    ]
    record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("email poll failed account=")
    )
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError
    assert str(record.exc_info[1]) == "exception details redacted"
    traceback_names = []
    traceback = record.exc_info[2]
    while traceback is not None:
        traceback_names.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "broken_poll" in traceback_names
    assert "error_type=RuntimeError" in caplog.text
    for sensitive_value in sensitive_values:
        assert sensitive_value not in caplog.text


async def test_email_poll_raw_imap_error_uses_safe_code_without_banner(monkeypatch, caplog):
    account = SimpleNamespace(id=uuid.uuid4(), tenant_id="tenant-a")
    claim = SimpleNamespace()
    failures = []

    async def checkpoint(**_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def claim_checkpoint(*_args, **_kwargs):
        return claim

    async def broken_poll(*_args, **_kwargs):
        raise imaplib.IMAP4.error(b"SECRET BANNER")

    async def fail_run(*_args, **kwargs):
        failures.append(kwargs)

    monkeypatch.setattr(email_poll, "ensure_checkpoint", checkpoint)
    monkeypatch.setattr(email_poll, "claim_checkpoint", claim_checkpoint)
    monkeypatch.setattr(email_poll, "_poll_account", broken_poll)
    monkeypatch.setattr(email_poll, "fail_run", fail_run)

    with caplog.at_level(logging.WARNING):
        result = await email_poll._poll_one_account(
            account,
            owner="owner",
            poll_interval_seconds=60,
            max_messages=1,
            network_timeout_seconds=1,
            allowed_hosts=frozenset({"imap.example.com"}),
            client_factory=lambda **_kwargs: None,
        )

    assert result == []
    assert failures == [
        {
            "error_code": "IMAP_PROTOCOL_ERROR",
            "error_message": "IMAP_PROTOCOL_ERROR",
            "retry_after_seconds": 60,
        }
    ]
    assert "account=" in caplog.text
    assert "code=IMAP_PROTOCOL_ERROR" in caplog.text
    assert "SECRET BANNER" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_email_poll_close_error_logs_only_safe_code_and_type(monkeypatch, caplog):
    account = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        config={
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "self_address": "support@example.com",
        },
        credential_bundle={"username": "support", "password": "password"},
    )
    claim = ClaimedCheckpoint(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        claim_token=uuid.uuid4(),
        revision=1,
        cursor=None,
        bootstrapped=False,
        mode="POLL",
        active_gap=None,
    )

    class Client:
        async def connect(self) -> int:
            return 42

        async def search_uids(self, *, start_uid: int = 1) -> tuple[int, ...]:
            return ()

        async def aclose(self) -> None:
            raise imaplib.IMAP4.error(b"SECRET BANNER")

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(email_poll, "require_claim", no_op)
    monkeypatch.setattr(email_poll, "_complete_checkpoint_or_raise", no_op)

    with caplog.at_level(logging.WARNING):
        result = await email_poll._poll_account(
            account,
            claim=claim,
            poll_interval_seconds=60,
            max_messages=10,
            network_timeout_seconds=10,
            allowed_hosts=frozenset({"imap.example.com"}),
            client_factory=lambda **_kwargs: Client(),
        )

    assert result == []
    assert "code=IMAP_CLOSE_FAILED" in caplog.text
    assert "error_type=error" in caplog.text
    assert "SECRET BANNER" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_email_checkpoint_false_completion_raises_lease_lost(monkeypatch):
    async def false_completion(*_args, **_kwargs):
        return False

    monkeypatch.setattr(email_poll, "complete_checkpoint", false_completion)
    claim = SimpleNamespace(id=uuid.uuid4())

    with pytest.raises(LeaseLostError):
        await email_poll._complete_checkpoint_or_raise(
            claim,
            cursor=None,
            bootstrapped=True,
            interval_seconds=60,
            page_count=1,
            occurrence_count=0,
        )


def test_email_cursor_round_trips_and_fails_closed():
    valid = EmailCursor(42, 7).serialize()
    assert EmailCursor.parse(valid) == EmailCursor(42, 7)

    invalid = (
        "42:7",
        "{}",
        '{"version":1,"uidvalidity":42,"last_uid":7,"extra":true}',
        '{"version":2,"uidvalidity":42,"last_uid":7}',
        '{"version":1,"uidvalidity":0,"last_uid":7}',
        '{"version":1,"uidvalidity":42,"last_uid":-1}',
        '{"version":true,"uidvalidity":42,"last_uid":7}',
    )
    for value in invalid:
        with pytest.raises(EmailCursorError):
            EmailCursor.parse(value)


async def test_email_new_uids_at_rfc_max_does_not_issue_reversing_search():
    class IdleClient:
        async def search_uids(self, *, start_uid: int = 1) -> tuple[int, ...]:
            raise AssertionError(f"must not search from invalid UID {start_uid}")

    assert await email_poll._new_uids(IdleClient(), 4294967295) == ()


def test_email_account_poll_budget_scales_but_stays_below_checkpoint_lease():
    assert (
        email_poll._account_poll_budget_seconds(
            network_timeout_seconds=1,
            max_messages=1,
        )
        == 5
    )
    assert (
        email_poll._account_poll_budget_seconds(
            network_timeout_seconds=1,
            max_messages=2,
        )
        == 7
    )
    assert (
        email_poll._account_poll_budget_seconds(
            network_timeout_seconds=120,
            max_messages=1000,
        )
        == email_poll._MAX_ACCOUNT_POLL_SECONDS
    )
    assert email_poll._MAX_ACCOUNT_POLL_SECONDS < email_poll._CHECKPOINT_LEASE_SECONDS


def test_email_network_error_codes_are_bounded_and_stable():
    assert (
        email_poll._bounded_stable_error_code(
            "email_dns_address_forbidden",
            fallback="EMAIL_NETWORK_FAILED",
        )
        == "email_dns_address_forbidden"
    )
    assert (
        email_poll._bounded_stable_error_code("x" * 129, fallback="EMAIL_NETWORK_FAILED")
        == "EMAIL_NETWORK_FAILED"
    )
    assert (
        email_poll._bounded_stable_error_code(
            "unsafe code with details",
            fallback="EMAIL_NETWORK_FAILED",
        )
        == "EMAIL_NETWORK_FAILED"
    )


def test_email_account_contract_reads_runtime_snapshot_and_preserves_account_text():
    account = SimpleNamespace(
        config={
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "self_address": "Support@Example.com",
            "mailbox": " Support ",
            "internal_domain_policy": "allow",
        },
        credential_bundle={"username": " support ", "password": " app-password "},
    )

    contract = _account_contract(account)

    assert contract.imap_host == "imap.example.com"
    assert contract.imap_port == 993
    assert contract.username == " support "
    assert contract.password == " app-password "
    assert contract.self_address == "Support@example.com"
    assert contract.mailbox == " Support "
    assert contract.internal_domain_policy == "allow"

    account.config = {**account.config, "imap_port": True}
    with pytest.raises(EmailPollAccountError, match="EMAIL_IMAP_PORT_INVALID"):
        _account_contract(account)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("username", "user\nname", "EMAIL_USERNAME_INVALID"),
        ("password", "p" * 513, "EMAIL_PASSWORD_INVALID"),
        ("mailbox", "INBOX\rInjected", "EMAIL_MAILBOX_INVALID"),
        ("mailbox", " " * 4, "EMAIL_MAILBOX_INVALID"),
    ],
)
def test_email_account_contract_rejects_invalid_account_text(field, value, code):
    account = SimpleNamespace(
        config={
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "self_address": "Support@example.com",
            "mailbox": "INBOX",
        },
        credential_bundle={"username": "support", "password": "app-password"},
    )
    values = account.credential_bundle if field in {"username", "password"} else account.config
    values[field] = value

    with pytest.raises(EmailPollAccountError, match=code):
        _account_contract(account)


def test_email_account_contract_rejects_malformed_self_addresses():
    account = SimpleNamespace(
        config={
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "self_address": "support@example.com",
        },
        credential_bundle={"username": "support", "password": "app-password"},
    )

    for value in ("a@@b", "a@!!!", "a..b@example.com", "a example@example.com"):
        account.config["self_address"] = value
        with pytest.raises(EmailPollAccountError, match="EMAIL_SELF_ADDRESS_INVALID"):
            _account_contract(account)
