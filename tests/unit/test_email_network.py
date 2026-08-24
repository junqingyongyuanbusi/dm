"""Email endpoint validation: hostname syntax, ports, and fail-closed DNS checks."""

import socket

import pytest

from social_reply.connectors.email.network import (
    EmailNetworkError,
    normalize_allowed_hosts,
    normalize_hostname,
    require_allowed_host,
    resolve_public_target,
    validate_port,
)


def _record(address: str) -> tuple:
    if ":" in address:
        return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 993, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 993))


def _resolver(*addresses: str):
    def getaddrinfo(host, port, *, family, type):
        assert host == "imap.example.com"
        assert port == 993
        assert family == socket.AF_UNSPEC
        assert type == socket.SOCK_STREAM
        return [_record(address) for address in addresses]

    return getaddrinfo


@pytest.mark.parametrize(
    "hostname",
    [
        "127.0.0.1",
        "127.1",
        "017700000001",
        "0x7f000001",
        "2130706433",
        "::1",
        "[::1]",
        "localhost",
        "LOCALHOST.",
        "foo.localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "imap.example.com/path",
        "user@imap.example.com",
        "imap_example.com",
        "-imap.example.com",
        "imap..example.com",
        "imap.example.com:993",
    ],
)
def test_normalize_hostname_rejects_literals_localhost_and_malicious_syntax(hostname):
    with pytest.raises(EmailNetworkError):
        normalize_hostname(hostname)


def test_normalize_hostname_canonicalizes_idna_case_and_trailing_dot():
    assert normalize_hostname(" BÜCHER.Example. ") == "xn--bcher-kva.example"


@pytest.mark.parametrize("port", [None, True, 0, -1, 65536, "993"])
def test_validate_port_rejects_invalid_values(port):
    with pytest.raises(EmailNetworkError) as excinfo:
        validate_port(port)
    assert excinfo.value.code == "email_port_invalid"


def test_email_host_allowlist_is_canonical_and_checked_before_dns():
    allowed = normalize_allowed_hosts(" IMAP.Example.COM.,smtp.example.com ")
    assert allowed == frozenset({"imap.example.com", "smtp.example.com"})
    assert require_allowed_host("IMAP.Example.com.", allowed) == "imap.example.com"
    with pytest.raises(EmailNetworkError) as excinfo:
        require_allowed_host("other.example.com", allowed)
    assert excinfo.value.code == "email_hostname_not_allowed"


def test_resolve_public_target_accepts_only_public_addresses_and_deduplicates():
    target = resolve_public_target(
        "IMAP.Example.com.",
        993,
        getaddrinfo=_resolver("8.8.8.8", "2606:4700:4700::1111", "8.8.8.8"),
    )

    assert target.hostname == "imap.example.com"
    assert target.port == 993
    assert tuple(str(address) for address in target.addresses) == (
        "8.8.8.8",
        "2606:4700:4700::1111",
    )


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "10.0.0.1",
        "127.0.0.1",
        "169.254.1.1",
        "192.0.2.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "2001:db8::1",
    ],
)
def test_resolve_public_target_rejects_non_global_address_classes(address):
    with pytest.raises(EmailNetworkError) as excinfo:
        resolve_public_target(
            "imap.example.com",
            993,
            getaddrinfo=_resolver(address),
        )
    assert excinfo.value.code == "email_dns_address_forbidden"


def test_resolve_public_target_rejects_mixed_public_and_private_dns_answers():
    with pytest.raises(EmailNetworkError) as excinfo:
        resolve_public_target(
            "imap.example.com",
            993,
            getaddrinfo=_resolver("8.8.8.8", "10.0.0.8"),
        )
    assert excinfo.value.code == "email_dns_address_forbidden"


def test_resolve_public_target_fails_closed_on_empty_or_malformed_dns_response():
    with pytest.raises(EmailNetworkError) as empty:
        resolve_public_target(
            "imap.example.com",
            993,
            getaddrinfo=lambda *_args, **_kwargs: [],
        )
    assert empty.value.code == "email_dns_resolution_failed"

    with pytest.raises(EmailNetworkError) as malformed:
        resolve_public_target(
            "imap.example.com",
            993,
            getaddrinfo=lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM)],
        )
    assert malformed.value.code == "email_dns_response_invalid"


def test_resolve_public_target_hides_resolver_error_details():
    def failing_resolver(*_args, **_kwargs):
        raise socket.gaierror("secret.internal.example")

    with pytest.raises(EmailNetworkError) as excinfo:
        resolve_public_target("imap.example.com", 993, getaddrinfo=failing_resolver)

    assert str(excinfo.value) == "email_dns_resolution_failed"
