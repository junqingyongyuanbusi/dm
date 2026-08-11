"""TLS-only IMAP client commands, strict response parsing, threading, and close behavior."""

import asyncio
import imaplib
import ssl
import threading

import pytest

from social_reply.connectors.email.imap_client import (
    EmailImapClient,
    ImapClientError,
    _imap_ssl_factory,
)
from social_reply.connectors.email.network import EmailNetworkError


class _FakeImap:
    def __init__(self) -> None:
        self.login_response = ("OK", [b"logged in"])
        self.select_response = ("OK", [b"3"])
        self.uidvalidity_response = ("UIDVALIDITY", [b"42"])
        self.search_response = ("OK", [b"7 9"])
        self.size_fetch_response = ("OK", [b"1 (UID 7 RFC822.SIZE 5)"])
        self.fetch_response = ("OK", [(b"1 (UID 7 BODY[] {5}", b"hello"), b")"])
        self.calls: list[tuple] = []
        self.thread_ids: list[int] = []
        self.logout_called = 0
        self.logout_error: Exception | None = None

    def _record(self, *call) -> None:
        self.calls.append(call)
        self.thread_ids.append(threading.get_ident())

    def login(self, user: str, password: str):
        self._record("login", user, password)
        return self.login_response

    def select(self, mailbox: str = "INBOX", readonly: bool = False):
        self._record("select", mailbox, readonly)
        return self.select_response

    def response(self, code: str):
        self._record("response", code)
        return self.uidvalidity_response

    def uid(self, command: str, *args):
        self._record("uid", command, *args)
        if command == "SEARCH":
            return self.search_response
        if command == "FETCH":
            if args[-1] == "(UID RFC822.SIZE)":
                return self.size_fetch_response
            return self.fetch_response
        raise AssertionError(f"unexpected command: {command}")

    def logout(self):
        self._record("logout")
        self.logout_called += 1
        if self.logout_error is not None:
            raise self.logout_error
        return "BYE", [b"logged out"]


def _client(fake: _FakeImap, *, events: list | None = None) -> EmailImapClient:
    events = events if events is not None else []

    def validate(host: str, port: int) -> None:
        events.append(("validate", host, port, threading.get_ident()))

    def factory(host: str, port: int, timeout: float) -> _FakeImap:
        events.append(("factory", host, port, timeout, threading.get_ident()))
        return fake

    return EmailImapClient(
        imap_host="imap.example.com",
        imap_port=993,
        username="support@example.com",
        password="app-password",
        mailbox="Support",
        timeout=7.5,
        imap_factory=factory,
        network_validator=validate,
        allowed_hosts=frozenset({"imap.example.com"}),
    )


def test_imap_ssl_factory_uses_verified_default_context_without_network_access():
    seen = {}

    def context_factory():
        context = ssl.create_default_context()
        seen["context"] = context
        return context

    def client_factory(**kwargs):
        seen["kwargs"] = kwargs
        return "sentinel"

    result = _imap_ssl_factory(
        "imap.example.com",
        993,
        7.0,
        context_factory=context_factory,
        client_factory=client_factory,
    )

    assert result == "sentinel"
    context = seen["context"]
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert seen["kwargs"] == {
        "host": "imap.example.com",
        "port": 993,
        "ssl_context": context,
        "timeout": 7.0,
    }


async def test_imap_connect_rechecks_network_then_logs_in_and_selects_readonly_off_loop():
    fake = _FakeImap()
    events = []
    client = _client(fake, events=events)
    event_loop_thread = threading.get_ident()

    uidvalidity = await client.connect()

    assert uidvalidity == 42
    assert client.uidvalidity == 42
    assert [event[0] for event in events] == ["validate", "factory"]
    assert events[0][1:3] == ("imap.example.com", 993)
    assert events[1][1:4] == ("imap.example.com", 993, 7.5)
    assert all(event[-1] != event_loop_thread for event in events)
    assert fake.calls[:3] == [
        ("login", "support@example.com", "app-password"),
        ("select", "Support", True),
        ("response", "UIDVALIDITY"),
    ]
    assert all(thread_id != event_loop_thread for thread_id in fake.thread_ids)


@pytest.mark.parametrize("cancellation_mode", ["cancel", "cancel_twice", "timeout"])
async def test_imap_blocking_connect_finishes_before_cancel_releases_lock(cancellation_mode):
    fake = _FakeImap()
    factory_started = threading.Event()
    release_factory = threading.Event()

    def blocking_factory(_host: str, _port: int, _timeout: float) -> _FakeImap:
        factory_started.set()
        if not release_factory.wait(timeout=2):
            raise AssertionError("test did not release blocking IMAP factory")
        return fake

    client = EmailImapClient(
        imap_host="imap.example.com",
        imap_port=993,
        username="support@example.com",
        password="app-password",
        imap_factory=blocking_factory,
        network_validator=lambda _host, _port: None,
        allowed_hosts=frozenset({"imap.example.com"}),
    )

    async def connect_then_close() -> None:
        try:
            if cancellation_mode == "timeout":
                async with asyncio.timeout(0.05):
                    await client.connect()
            else:
                await client.connect()
        finally:
            await client.aclose()

    task = asyncio.create_task(connect_then_close())
    async with asyncio.timeout(1):
        while not factory_started.is_set():
            await asyncio.sleep(0)

    if cancellation_mode.startswith("cancel"):
        task.cancel()
        await asyncio.sleep(0)
        if cancellation_mode == "cancel_twice":
            task.cancel()
    await asyncio.sleep(0.1)
    try:
        assert not task.done()
    finally:
        release_factory.set()

    expected_error = (
        asyncio.CancelledError if cancellation_mode.startswith("cancel") else TimeoutError
    )
    with pytest.raises(expected_error):
        await task

    assert fake.logout_called == 1
    assert client.uidvalidity is None
    await asyncio.sleep(0.01)
    assert fake.logout_called == 1
    assert client.uidvalidity is None


async def test_imap_search_and_fetch_use_uid_commands_and_strict_payloads():
    fake = _FakeImap()
    client = _client(fake)
    await client.connect()

    uids = await client.search_uids(start_uid=7)
    size = await client.fetch_message_size(7)
    raw = await client.fetch_message(7)

    assert uids == (7, 9)
    assert size == 5
    assert raw == b"hello"
    assert ("uid", "SEARCH", None, "UID", "7:4294967295") in fake.calls
    assert ("uid", "FETCH", "7", "(UID RFC822.SIZE)") in fake.calls
    assert ("uid", "FETCH", "7", "(UID BODY.PEEK[])") in fake.calls


async def test_imap_idle_mailbox_at_max_uid_uses_non_reversing_closed_range():
    fake = _FakeImap()
    fake.search_response = ("OK", [b""])
    client = _client(fake)
    await client.connect()

    assert await client.search_uids(start_uid=4294967295) == ()
    assert ("uid", "SEARCH", None, "UID", "4294967295:4294967295") in fake.calls


async def test_imap_connect_rejects_non_allowlisted_host_before_dns():
    fake = _FakeImap()
    validations = []
    client = EmailImapClient(
        imap_host="imap.example.com",
        imap_port=993,
        username="user",
        password="password",
        imap_factory=lambda _host, _port, _timeout: fake,
        network_validator=lambda host, port: validations.append((host, port)),
        allowed_hosts=frozenset({"other.example.com"}),
    )

    with pytest.raises(EmailNetworkError, match="email_hostname_not_allowed"):
        await client.connect()
    assert validations == []
    assert fake.calls == []


async def test_imap_connect_revalidates_before_every_new_connection():
    first = _FakeImap()
    second = _FakeImap()
    fakes = iter((first, second))
    validations = []

    def validate(host: str, port: int) -> None:
        validations.append((host, port))

    client = EmailImapClient(
        imap_host="imap.example.com",
        imap_port=993,
        username="user",
        password="password",
        imap_factory=lambda _host, _port, _timeout: next(fakes),
        network_validator=validate,
        allowed_hosts=frozenset({"imap.example.com"}),
    )

    await client.connect()
    await client.aclose()
    await client.connect()

    assert validations == [("imap.example.com", 993), ("imap.example.com", 993)]
    assert first.logout_called == 1


@pytest.mark.parametrize(
    "uidvalidity_response",
    [
        (None, [b"42"]),
        ("UIDVALIDITY", []),
        ("UIDVALIDITY", [b"0"]),
        ("UIDVALIDITY", [b"01"]),
        ("UIDVALIDITY", [b"4294967296"]),
        ("UIDVALIDITY", [b"42", b"43"]),
    ],
)
async def test_imap_uidvalidity_is_parsed_strictly_and_failed_connect_logs_out(
    uidvalidity_response,
):
    fake = _FakeImap()
    fake.uidvalidity_response = uidvalidity_response
    client = _client(fake)

    with pytest.raises(ImapClientError) as excinfo:
        await client.connect()

    assert excinfo.value.code == "imap_uidvalidity_invalid"
    assert fake.logout_called == 1
    assert client.uidvalidity is None


@pytest.mark.parametrize(
    "search_response",
    [
        ("NO", [b"7"]),
        ("OK", []),
        ("OK", [b"7 7"]),
        ("OK", [b"9 7"]),
        ("OK", [b"6 7"]),
        ("OK", [b"07"]),
        ("OK", [b"7 x"]),
        ("OK", [b"7", b"9"]),
    ],
)
async def test_imap_search_response_fails_closed(search_response):
    fake = _FakeImap()
    fake.search_response = search_response
    client = _client(fake)
    await client.connect()

    with pytest.raises(ImapClientError):
        await client.search_uids(start_uid=7)


async def test_imap_size_fetch_accepts_attributes_in_reverse_order():
    fake = _FakeImap()
    fake.size_fetch_response = ("OK", [b"1 (RFC822.SIZE 5 UID 7)"])
    client = _client(fake)
    await client.connect()

    assert await client.fetch_message_size(7) == 5


@pytest.mark.parametrize(
    "size_fetch_response",
    [
        ("NO", []),
        ("OK", []),
        ("OK", [b"1 (UID 8 RFC822.SIZE 5)"]),
        ("OK", [b"1 (UID 7 RFC822.SIZE 05)"]),
        ("OK", [b"1 (UID 7 RFC822.SIZE -1)"]),
        ("OK", [b"1 (UID 7 UID 7 RFC822.SIZE 5)"]),
        ("OK", [b"1 (UID 7 RFC822.SIZE 5 RFC822.SIZE 5)"]),
        ("OK", [b"1 (UID 7 RFC822.SIZE 5 FLAGS seen)"]),
        ("OK", [b"1 (UID 7 RFC822.SIZE 5)", b"extra"]),
    ],
)
async def test_imap_size_fetch_response_fails_closed(size_fetch_response):
    fake = _FakeImap()
    fake.size_fetch_response = size_fetch_response
    client = _client(fake)
    await client.connect()

    with pytest.raises(ImapClientError):
        await client.fetch_message_size(7)


async def test_imap_body_fetch_accepts_uid_after_literal():
    fake = _FakeImap()
    fake.fetch_response = ("OK", [(b"1 (BODY[] {5}", b"hello"), b" UID 7)"])
    client = _client(fake)
    await client.connect()

    assert await client.fetch_message(7) == b"hello"


@pytest.mark.parametrize(
    "fetch_response",
    [
        ("NO", []),
        ("OK", []),
        ("OK", [(b"1 (UID 8 BODY[] {5}", b"hello"), b")"]),
        ("OK", [(b"1 (UID 7 BODY[] {4}", b"hello"), b")"]),
        ("OK", [(b"1 (UID 7 BODY[TEXT] {5}", b"hello"), b")"]),
        ("OK", [(b"1 (UID 7 BODY[] {5}", "hello"), b")"]),
        ("OK", [(b"1 (UID 7 BODY[] {5}", b"hello")]),
        ("OK", [(b"1 (UID 7 BODY[] {5}", b"hello"), b")", b"extra"]),
        ("OK", [(b"1 (UID 7 BODY[] {5}", b"hello"), b" UID 7)"]),
        ("OK", [(b"1 (BODY[] {5}", b"hello"), b" UID 7 UID 7)"]),
        ("OK", [(b"1 (BODY[] {5}", b"hello"), b" FLAGS seen UID 7)"]),
        ("OK", [(b"1 (UID 7 BODY[] BODY[] {5}", b"hello"), b")"]),
    ],
)
async def test_imap_fetch_response_fails_closed(fetch_response):
    fake = _FakeImap()
    fake.fetch_response = fetch_response
    client = _client(fake)
    await client.connect()

    with pytest.raises(ImapClientError):
        await client.fetch_message(7)


@pytest.mark.parametrize(
    ("boundary", "expected_code"),
    [
        ("connect", "imap_connect_protocol_error"),
        ("login", "imap_authentication_failed"),
        ("select", "imap_select_failed"),
        ("logout", "imap_logout_failed"),
    ],
)
async def test_imap_library_errors_are_mapped_without_provider_banner(boundary, expected_code):
    fake = _FakeImap()
    secret_banner = b"SECRET BANNER"

    def raise_banner(*_args, **_kwargs):
        raise imaplib.IMAP4.error(secret_banner)

    if boundary == "connect":
        client = EmailImapClient(
            imap_host="imap.example.com",
            imap_port=993,
            username="support@example.com",
            password="app-password",
            imap_factory=raise_banner,
            network_validator=lambda _host, _port: None,
            allowed_hosts=frozenset({"imap.example.com"}),
        )
    else:
        setattr(fake, boundary, raise_banner)
        client = _client(fake)
        if boundary == "logout":
            await client.connect()

    with pytest.raises(ImapClientError) as excinfo:
        if boundary == "logout":
            await client.logout()
        else:
            await client.connect()

    assert excinfo.value.code == expected_code
    assert excinfo.value.retryable is False
    assert "SECRET BANNER" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (TimeoutError("SECRET BANNER"), "imap_connection_timeout"),
        (OSError("SECRET BANNER"), "imap_transport_failed"),
    ],
)
async def test_imap_connect_transport_failures_are_stable_and_retryable(failure, expected_code):
    def fail_connect(*_args):
        raise failure

    client = EmailImapClient(
        imap_host="imap.example.com",
        imap_port=993,
        username="support@example.com",
        password="app-password",
        imap_factory=fail_connect,
        network_validator=lambda _host, _port: None,
        allowed_hosts=frozenset({"imap.example.com"}),
    )

    with pytest.raises(ImapClientError) as excinfo:
        await client.connect()

    assert excinfo.value.code == expected_code
    assert excinfo.value.retryable is True
    assert "SECRET BANNER" not in str(excinfo.value)


@pytest.mark.parametrize(
    "failure",
    [
        ssl.SSLCertVerificationError("SECRET CERTIFICATE DETAIL"),
        ssl.CertificateError("SECRET HOSTNAME DETAIL"),
        ssl.SSLError("SECRET TLS ALERT DETAIL"),
    ],
)
async def test_imap_tls_connection_failures_are_stable_and_nonretryable(failure):
    def fail_connect(*_args):
        raise failure

    client = EmailImapClient(
        imap_host="imap.example.com",
        imap_port=993,
        username="support@example.com",
        password="app-password",
        imap_factory=fail_connect,
        network_validator=lambda _host, _port: None,
        allowed_hosts=frozenset({"imap.example.com"}),
    )

    with pytest.raises(ImapClientError) as excinfo:
        await client.connect()

    assert excinfo.value.code == "imap_tls_invalid"
    assert excinfo.value.retryable is False
    assert "SECRET" not in str(excinfo.value)


@pytest.mark.parametrize("boundary", ["login", "select"])
async def test_imap_handshake_ssl_errors_are_stable_and_nonretryable(boundary):
    fake = _FakeImap()

    def fail_tls(*_args, **_kwargs):
        raise ssl.SSLError("SECRET provider TLS alert")

    setattr(fake, boundary, fail_tls)
    client = _client(fake)

    with pytest.raises(ImapClientError) as excinfo:
        await client.connect()

    assert excinfo.value.code == "imap_tls_invalid"
    assert excinfo.value.retryable is False
    assert str(excinfo.value) == "imap_tls_invalid"
    assert fake.logout_called == 1


async def test_imap_login_or_select_failure_fails_closed_and_logs_out():
    for attribute, response, code in (
        ("login_response", ("NO", [b"denied"]), "imap_authentication_failed"),
        ("select_response", ("NO", [b"denied"]), "imap_select_failed"),
    ):
        fake = _FakeImap()
        setattr(fake, attribute, response)
        client = _client(fake)

        with pytest.raises(ImapClientError) as excinfo:
            await client.connect()

        assert excinfo.value.code == code
        assert fake.logout_called == 1


async def test_imap_operations_require_connection_and_validate_uids():
    client = _client(_FakeImap())

    with pytest.raises(ImapClientError, match="imap_not_connected"):
        await client.search_uids()
    with pytest.raises(ImapClientError, match="imap_uid_invalid"):
        await client.fetch_message_size(0)
    with pytest.raises(ImapClientError, match="imap_uid_invalid"):
        await client.fetch_message(0)
    with pytest.raises(ImapClientError, match="imap_uid_invalid"):
        await client.search_uids(start_uid=True)


@pytest.mark.parametrize(
    "logout_error, suppressed",
    [
        (OSError("socket closed"), True),
        (imaplib.IMAP4.error("connection lost"), True),
        (RuntimeError("unexpected"), False),
    ],
)
async def test_imap_aclose_only_suppresses_expected_transport_errors(logout_error, suppressed):
    fake = _FakeImap()
    client = _client(fake)
    await client.connect()
    fake.logout_error = logout_error

    if suppressed:
        await client.aclose()
    else:
        with pytest.raises(RuntimeError, match="unexpected"):
            await client.aclose()

    assert fake.logout_called == 1
    assert client.uidvalidity is None


async def test_imap_aclose_logs_out_off_loop_and_clears_state():
    fake = _FakeImap()
    client = _client(fake)
    await client.connect()
    event_loop_thread = threading.get_ident()

    await client.aclose()

    assert fake.logout_called == 1
    assert fake.thread_ids[-1] != event_loop_thread
    assert client.uidvalidity is None
    await client.aclose()
    assert fake.logout_called == 1
