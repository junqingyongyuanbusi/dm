"""Async facade over a strictly parsed, TLS-only standard-library IMAP client."""

import asyncio
import imaplib
import re
import ssl
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from social_reply.connectors.email.network import (
    DEFAULT_EMAIL_ALLOWED_HOSTS,
    normalize_allowed_hosts,
    normalize_hostname,
    require_allowed_host,
    resolve_public_target,
    validate_port,
)

_MAX_IMAP_NUMBER = (1 << 32) - 1
_MAX_MESSAGE_SIZE_NUMBER = (1 << 63) - 1
_FETCH_RESPONSE = re.compile(rb"^[1-9][0-9]* \((?P<attributes>.*)\)$")
_FETCH_LITERAL_HEADER = re.compile(
    rb"^[1-9][0-9]* \((?P<attributes>.+) \{(?P<size>0|[1-9][0-9]*)\}$"
)


class _ImapConnection(Protocol):
    def login(self, user: str, password: str) -> tuple[str, list[bytes]]: ...

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]: ...

    def response(self, code: str) -> tuple[str | None, list[bytes]]: ...

    def uid(self, command: str, *args: str | None) -> tuple[str, list[Any]]: ...

    def logout(self) -> tuple[str, list[bytes]]: ...


type _ImapFactory = Callable[[str, int, float], _ImapConnection]
type _NetworkValidator = Callable[[str, int], object]
_BlockingResult = TypeVar("_BlockingResult")


class ImapClientError(RuntimeError):
    """A stable, provider-detail-free IMAP failure."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def _imap_ssl_factory(
    host: str,
    port: int,
    timeout: float,
    *,
    context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
    client_factory: Callable[..., _ImapConnection] = imaplib.IMAP4_SSL,
) -> _ImapConnection:
    context = context_factory()
    return client_factory(
        host=host,
        port=port,
        ssl_context=context,
        timeout=timeout,
    )


class EmailImapClient:
    """Serialized async access to one authenticated, read-only IMAP mailbox.

    Every blocking connection transaction or IMAP command runs wholly inside ``asyncio.to_thread``.
    The injected factory and network validator make polling code and unit tests independent of real
    network access.
    """

    def __init__(
        self,
        *,
        imap_host: str,
        imap_port: int,
        username: str,
        password: str,
        mailbox: str = "INBOX",
        timeout: float = 10.0,
        imap_factory: _ImapFactory = _imap_ssl_factory,
        network_validator: _NetworkValidator = resolve_public_target,
        allowed_hosts: frozenset[str] = DEFAULT_EMAIL_ALLOWED_HOSTS,
    ) -> None:
        self._imap_host = normalize_hostname(imap_host)
        self._imap_port = validate_port(imap_port)
        self._username = username
        self._password = password
        self._mailbox = mailbox
        self._timeout = timeout
        self._imap_factory = imap_factory
        self._network_validator = network_validator
        self._allowed_hosts = normalize_allowed_hosts(allowed_hosts)
        self._imap: _ImapConnection | None = None
        self._uidvalidity: int | None = None
        self._lock = asyncio.Lock()

    @property
    def uidvalidity(self) -> int | None:
        return self._uidvalidity

    async def connect(self) -> int:
        return await self._run_blocking(self._connect)

    async def search_uids(self, *, start_uid: int = 1) -> tuple[int, ...]:
        return await self._run_blocking(self._search_uids, _validate_uid(start_uid))

    async def fetch_message_size(self, uid: int) -> int:
        return await self._run_blocking(self._fetch_message_size, _validate_uid(uid))

    async def fetch_message(self, uid: int) -> bytes:
        return await self._run_blocking(self._fetch_message, _validate_uid(uid))

    async def logout(self) -> None:
        await self._run_blocking(self._logout)

    async def _run_blocking(
        self,
        function: Callable[..., _BlockingResult],
        /,
        *args: object,
    ) -> _BlockingResult:
        async with self._lock:
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                task.result()
                raise

    async def aclose(self) -> None:
        try:
            await self.logout()
        except ImapClientError:
            # Closing an already-broken IMAP connection is best effort and must not
            # replace the completed poll result or the operation error being handled.
            pass

    def _connect(self) -> int:
        if self._imap is not None:
            if self._uidvalidity is None:
                raise ImapClientError("imap_state_invalid")
            return self._uidvalidity

        # Enforce deployment policy before DNS, then re-resolve fail-closed immediately before
        # every new high-level client connection. Standard-library IMAP cannot reliably pin this
        # checked address set to its later socket; see network.py.
        require_allowed_host(self._imap_host, self._allowed_hosts)
        self._network_validator(self._imap_host, self._imap_port)
        imap = _call_imap(
            "imap_connect_protocol_error",
            self._imap_factory,
            self._imap_host,
            self._imap_port,
            self._timeout,
        )
        try:
            _require_ok(
                _call_imap(
                    "imap_authentication_failed",
                    imap.login,
                    self._username,
                    self._password,
                ),
                "imap_authentication_failed",
            )
            _parse_select(
                _call_imap("imap_select_failed", imap.select, self._mailbox, readonly=True)
            )
            uidvalidity = _parse_uidvalidity(
                _call_imap("imap_uidvalidity_failed", imap.response, "UIDVALIDITY")
            )
        except Exception:
            _best_effort_logout(imap)
            raise

        self._imap = imap
        self._uidvalidity = uidvalidity
        return uidvalidity

    def _search_uids(self, start_uid: int) -> tuple[int, ...]:
        imap = self._require_connection()
        response = _call_imap(
            "imap_search_failed",
            imap.uid,
            "SEARCH",
            None,
            "UID",
            f"{start_uid}:{_MAX_IMAP_NUMBER}",
        )
        return _parse_search_response(response, start_uid=start_uid)

    def _fetch_message_size(self, uid: int) -> int:
        imap = self._require_connection()
        response = _call_imap(
            "imap_fetch_failed",
            imap.uid,
            "FETCH",
            str(uid),
            "(UID RFC822.SIZE)",
        )
        return _parse_size_fetch_response(response, expected_uid=uid)

    def _fetch_message(self, uid: int) -> bytes:
        imap = self._require_connection()
        response = _call_imap(
            "imap_fetch_failed",
            imap.uid,
            "FETCH",
            str(uid),
            "(UID BODY.PEEK[])",
        )
        return _parse_fetch_response(response, expected_uid=uid)

    def _logout(self) -> None:
        imap = self._imap
        self._imap = None
        self._uidvalidity = None
        if imap is not None:
            _call_imap("imap_logout_failed", imap.logout)

    def _require_connection(self) -> _ImapConnection:
        if self._imap is None or self._uidvalidity is None:
            raise ImapClientError("imap_not_connected")
        return self._imap


def _call_imap[Result](
    protocol_code: str,
    function: Callable[..., Result],
    /,
    *args: object,
    **kwargs: object,
) -> Result:
    try:
        return function(*args, **kwargs)
    except imaplib.IMAP4.error:
        raise ImapClientError(protocol_code) from None
    except TimeoutError:
        raise ImapClientError("imap_connection_timeout", retryable=True) from None
    except ssl.SSLError:
        raise ImapClientError("imap_tls_invalid", retryable=False) from None
    except OSError:
        raise ImapClientError("imap_transport_failed", retryable=True) from None


def _require_ok(response: object, code: str) -> list[Any]:
    if not isinstance(response, tuple) or len(response) != 2:
        raise ImapClientError("imap_response_invalid")
    status, data = response
    if status != "OK":
        raise ImapClientError(code)
    if not isinstance(data, list):
        raise ImapClientError("imap_response_invalid")
    return data


def _parse_select(response: object) -> int:
    data = _require_ok(response, "imap_select_failed")
    if len(data) != 1 or not isinstance(data[0], bytes):
        raise ImapClientError("imap_select_response_invalid")
    return _parse_decimal(data[0], code="imap_select_response_invalid", allow_zero=True)


def _parse_uidvalidity(response: object) -> int:
    if not isinstance(response, tuple) or len(response) != 2:
        raise ImapClientError("imap_uidvalidity_invalid")
    code, data = response
    if code != "UIDVALIDITY" or not isinstance(data, list) or len(data) != 1:
        raise ImapClientError("imap_uidvalidity_invalid")
    value = data[0]
    if not isinstance(value, bytes):
        raise ImapClientError("imap_uidvalidity_invalid")
    return _parse_decimal(value, code="imap_uidvalidity_invalid", allow_zero=False)


def _parse_search_response(response: object, *, start_uid: int) -> tuple[int, ...]:
    data = _require_ok(response, "imap_search_failed")
    if len(data) != 1 or not isinstance(data[0], bytes):
        raise ImapClientError("imap_search_response_invalid")
    if not data[0]:
        return ()

    values: list[int] = []
    previous = 0
    for token in data[0].split(b" "):
        uid = _parse_decimal(token, code="imap_search_response_invalid", allow_zero=False)
        if uid < start_uid or uid <= previous:
            raise ImapClientError("imap_search_response_invalid")
        values.append(uid)
        previous = uid
    return tuple(values)


def _parse_size_fetch_response(response: object, *, expected_uid: int) -> int:
    data = _require_ok(response, "imap_fetch_failed")
    if len(data) != 1 or not isinstance(data[0], bytes):
        raise ImapClientError("imap_fetch_response_invalid")
    match = _FETCH_RESPONSE.fullmatch(data[0])
    if match is None:
        raise ImapClientError("imap_fetch_response_invalid")
    attributes = _parse_fetch_attributes(
        match.group("attributes"),
        allowed=frozenset({b"UID", b"RFC822.SIZE"}),
    )
    if set(attributes) != {b"UID", b"RFC822.SIZE"}:
        raise ImapClientError("imap_fetch_response_invalid")
    response_uid = _parse_decimal(
        attributes[b"UID"],
        code="imap_fetch_response_invalid",
        allow_zero=False,
    )
    message_size = _parse_decimal(
        attributes[b"RFC822.SIZE"],
        code="imap_fetch_response_invalid",
        allow_zero=True,
        maximum=_MAX_MESSAGE_SIZE_NUMBER,
    )
    if response_uid != expected_uid:
        raise ImapClientError("imap_fetch_response_invalid")
    return message_size


def _parse_fetch_response(response: object, *, expected_uid: int) -> bytes:
    data = _require_ok(response, "imap_fetch_failed")
    if len(data) != 2:
        raise ImapClientError("imap_fetch_response_invalid")
    item, trailer = data
    if not isinstance(item, tuple) or len(item) != 2 or not isinstance(trailer, bytes):
        raise ImapClientError("imap_fetch_response_invalid")
    header, body = item
    if not isinstance(header, bytes) or not isinstance(body, bytes):
        raise ImapClientError("imap_fetch_response_invalid")
    match = _FETCH_LITERAL_HEADER.fullmatch(header)
    if match is None:
        raise ImapClientError("imap_fetch_response_invalid")

    header_tokens = match.group("attributes").split(b" ")
    if not header_tokens or header_tokens[-1] != b"BODY[]":
        raise ImapClientError("imap_fetch_response_invalid")
    attributes = _parse_fetch_attributes(
        b" ".join(header_tokens[:-1]),
        allowed=frozenset({b"UID"}),
    )
    if trailer == b")":
        trailer_attributes = b""
    elif trailer.startswith(b" ") and trailer.endswith(b")"):
        trailer_attributes = trailer[1:-1]
    else:
        raise ImapClientError("imap_fetch_response_invalid")
    for name, value in _parse_fetch_attributes(
        trailer_attributes,
        allowed=frozenset({b"UID"}),
    ).items():
        if name in attributes:
            raise ImapClientError("imap_fetch_response_invalid")
        attributes[name] = value
    if set(attributes) != {b"UID"}:
        raise ImapClientError("imap_fetch_response_invalid")

    response_uid = _parse_decimal(
        attributes[b"UID"],
        code="imap_fetch_response_invalid",
        allow_zero=False,
    )
    literal_size = _parse_decimal(
        match.group("size"),
        code="imap_fetch_response_invalid",
        allow_zero=True,
        maximum=None,
    )
    if response_uid != expected_uid or literal_size != len(body):
        raise ImapClientError("imap_fetch_response_invalid")
    return body


def _parse_fetch_attributes(value: bytes, *, allowed: frozenset[bytes]) -> dict[bytes, bytes]:
    if not value:
        return {}
    tokens = value.split(b" ")
    if len(tokens) % 2 or any(not token for token in tokens):
        raise ImapClientError("imap_fetch_response_invalid")
    attributes: dict[bytes, bytes] = {}
    for index in range(0, len(tokens), 2):
        name, attribute_value = tokens[index : index + 2]
        if name not in allowed or name in attributes:
            raise ImapClientError("imap_fetch_response_invalid")
        attributes[name] = attribute_value
    return attributes


def _validate_uid(value: int) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_IMAP_NUMBER:
        raise ImapClientError("imap_uid_invalid")
    return value


def _parse_decimal(
    value: bytes,
    *,
    code: str,
    allow_zero: bool,
    maximum: int | None = _MAX_IMAP_NUMBER,
) -> int:
    if not value or not value.isdigit() or (len(value) > 1 and value.startswith(b"0")):
        raise ImapClientError(code)
    parsed = int(value)
    if (parsed == 0 and not allow_zero) or (maximum is not None and parsed > maximum):
        raise ImapClientError(code)
    return parsed


def _best_effort_logout(imap: _ImapConnection) -> None:
    try:
        imap.logout()
    except (imaplib.IMAP4.error, OSError):
        pass
