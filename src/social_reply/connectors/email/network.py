"""Fail-closed network target validation for email protocol clients."""

import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

type _IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type _GetAddrInfo = Callable[..., list[tuple[int, int, int, str, tuple[Any, ...]]]]
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LOCALHOST_EQUIVALENTS = frozenset(
    {
        "ip6-localhost",
        "ip6-loopback",
        "localhost",
        "localhost.localdomain",
    }
)
DEFAULT_EMAIL_ALLOWED_HOSTS = frozenset({"imap.larksuite.com", "smtp.larksuite.com"})


class EmailNetworkError(ValueError):
    """A configured email endpoint is unsafe or cannot be validated."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ResolvedNetworkTarget:
    hostname: str
    port: int
    addresses: tuple[_IPAddress, ...]


def normalize_hostname(value: str) -> str:
    """Return a canonical ASCII hostname, rejecting literals and localhost aliases."""

    if not isinstance(value, str):
        raise EmailNetworkError("email_hostname_invalid")
    hostname = value.strip()
    if not hostname or "\x00" in hostname:
        raise EmailNetworkError("email_hostname_invalid")
    if hostname.endswith("."):
        hostname = hostname[:-1]
    if not hostname:
        raise EmailNetworkError("email_hostname_invalid")

    try:
        hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise EmailNetworkError("email_hostname_invalid") from exc

    if len(hostname) > 253:
        raise EmailNetworkError("email_hostname_invalid")
    labels = hostname.split(".")
    if any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise EmailNetworkError("email_hostname_invalid")
    if hostname in _LOCALHOST_EQUIVALENTS or hostname.endswith(".localhost"):
        raise EmailNetworkError("email_hostname_forbidden")
    if _is_ip_literal(hostname):
        raise EmailNetworkError("email_hostname_forbidden")
    return hostname


def validate_port(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise EmailNetworkError("email_port_invalid")
    return value


def normalize_allowed_hosts(values: object) -> frozenset[str]:
    if isinstance(values, str):
        candidates = values.split(",")
    elif isinstance(values, (set, frozenset, list, tuple)):
        candidates = values
    else:
        raise EmailNetworkError("email_allowed_hosts_invalid")
    try:
        normalized = frozenset(
            normalize_hostname(value) for value in candidates if str(value).strip()
        )
    except (EmailNetworkError, TypeError) as exc:
        raise EmailNetworkError("email_allowed_hosts_invalid") from exc
    return normalized


def require_allowed_host(hostname: str, allowed_hosts: frozenset[str]) -> str:
    normalized_hostname = normalize_hostname(hostname)
    if normalized_hostname not in allowed_hosts:
        raise EmailNetworkError("email_hostname_not_allowed")
    return normalized_hostname


def resolve_public_target(
    hostname: str,
    port: int,
    *,
    getaddrinfo: _GetAddrInfo = socket.getaddrinfo,
) -> ResolvedNetworkTarget:
    """Resolve an email endpoint and require every returned address to be public.

    ``imaplib`` and ``smtplib`` do not expose a supported way to pin their connection to the
    exact addresses checked here. Callers therefore must run this fail-closed check immediately
    before every new connection. This narrows DNS rebinding exposure but cannot eliminate the
    resolver-to-connect race in standard-library high-level clients. We deliberately do not
    monkeypatch their socket internals.
    """

    normalized_hostname = normalize_hostname(hostname)
    validated_port = validate_port(port)
    try:
        records = getaddrinfo(
            normalized_hostname,
            validated_port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise EmailNetworkError("email_dns_resolution_failed") from exc
    if not records:
        raise EmailNetworkError("email_dns_resolution_failed")

    addresses: set[_IPAddress] = set()
    for record in records:
        address = _address_from_record(record)
        if not _is_public_address(address):
            raise EmailNetworkError("email_dns_address_forbidden")
        addresses.add(address)
    if not addresses:
        raise EmailNetworkError("email_dns_resolution_failed")

    return ResolvedNetworkTarget(
        hostname=normalized_hostname,
        port=validated_port,
        addresses=tuple(sorted(addresses, key=lambda item: (item.version, int(item)))),
    )


def _is_public_address(address: _IPAddress) -> bool:
    return address.is_global and not any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_reserved,
            address.is_multicast,
            address.is_unspecified,
        )
    )


def _is_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return True

    # Legacy IPv4 spellings (integer, octal, hexadecimal, or shortened dotted forms) are
    # accepted by some resolvers even though ipaddress intentionally rejects them.
    try:
        socket.inet_aton(hostname)
    except OSError:
        return False
    return True


def _address_from_record(record: tuple[Any, ...]) -> _IPAddress:
    if len(record) != 5:
        raise EmailNetworkError("email_dns_response_invalid")
    family, _socktype, _protocol, _canonical_name, sockaddr = record
    if family not in {socket.AF_INET, socket.AF_INET6}:
        raise EmailNetworkError("email_dns_response_invalid")
    if not isinstance(sockaddr, tuple) or not sockaddr or not isinstance(sockaddr[0], str):
        raise EmailNetworkError("email_dns_response_invalid")
    try:
        address = ipaddress.ip_address(sockaddr[0])
    except ValueError as exc:
        raise EmailNetworkError("email_dns_response_invalid") from exc
    if (family == socket.AF_INET and address.version != 4) or (
        family == socket.AF_INET6 and address.version != 6
    ):
        raise EmailNetworkError("email_dns_response_invalid")
    return address
