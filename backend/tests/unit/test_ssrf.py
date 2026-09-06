"""Server-side request forgery protection for outbound page fetches.

Every other outbound request in StockBrain goes to a hostname the operator
configured.  This one does not: the URL comes from a search result, which came
from the open web, which means an attacker who can rank a page can choose an
address this process will connect to.  Inside this deployment's Docker network
that address could be ``postgres:5432``.

Each test names the specific attack it refuses.  A regression in any of them is
a remote attacker choosing an internal destination, so none of them is
cosmetic.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import pytest

from stockbrain.extraction.ssrf import (
    ALLOWED_SCHEMES,
    SsrfRefused,
    redact_url,
    verify_public_url,
)


@pytest.fixture
def resolves_to(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pin what a hostname resolves to.

    Name resolution is attacker-controlled -- ``evil.example`` can publish an A
    record of ``127.0.0.1`` -- so every test that matters here is a test about
    what the *resolved address* is, not about what the hostname looks like.
    """

    def _pin(*addresses: str) -> None:
        def fake_getaddrinfo(host: str, port: int, *args: object, **kwargs: object) -> list[Any]:
            return [
                (
                    socket.AF_INET6 if ":" in address else socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (address, port),
                )
                for address in addresses
            ]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    return _pin


# ---------------------------------------------------------------------------
# Schemes
# ---------------------------------------------------------------------------
def test_only_http_and_https_are_allowed() -> None:
    assert frozenset({"http", "https"}) == ALLOWED_SCHEMES


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "file://localhost/etc/shadow",
        "ftp://ftp.example.com/x",
        "gopher://example.com/",
        "data:text/html,<script>alert(1)</script>",
        "jar:http://example.com!/",
        "dict://127.0.0.1:11211/",
        "//example.com/no-scheme",
    ],
)
def test_a_non_http_scheme_is_refused(url: str) -> None:
    """``file://`` is how a "page fetcher" becomes a local file reader.

    Refused before any resolution happens, so nothing is looked up either.
    """
    with pytest.raises(SsrfRefused, match="not http or https"):
        verify_public_url(url)


def test_a_url_with_embedded_credentials_is_refused() -> None:
    """A fetcher must never present credentials handed to it by a web page."""
    with pytest.raises(SsrfRefused, match="embedded credentials"):
        verify_public_url("https://user:hunter2@example.com/a")


def test_a_url_with_no_host_is_refused() -> None:
    with pytest.raises(SsrfRefused):
        verify_public_url("http:///path-only")


def test_an_unparseable_url_is_refused_rather_than_raising() -> None:
    with pytest.raises(SsrfRefused):
        verify_public_url("http://[oops")


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://127.0.0.1/x", "loopback"),
        ("http://127.1.2.3/x", "loopback"),
        ("http://[::1]/x", "loopback"),
        ("http://0.0.0.0/x", "unspecified"),
        ("http://10.0.0.5/x", "private"),
        ("http://172.16.4.4/x", "private"),
        ("http://192.168.1.1/x", "private"),
        # Carrier-grade NAT (RFC 6598). Python's `is_private` disagrees with
        # itself across versions here, so it is the catch-all `is_global` check
        # that refuses it -- which is exactly why that check exists rather than
        # a hand-written list of ranges.
        ("http://100.64.0.1/x", "private address|non-global address"),
        ("http://[fc00::1]/x", "private"),
        ("http://[fd12:3456::1]/x", "private"),
        ("http://169.254.1.1/x", "link-local"),
        ("http://[fe80::1]/x", "link-local"),
        ("http://224.0.0.1/x", "multicast"),
        ("http://169.254.169.254/latest/meta-data/", "cloud instance metadata"),
        ("http://169.254.170.2/v2/credentials", "cloud instance metadata"),
        ("http://100.100.100.200/latest/", "cloud instance metadata"),
        ("http://[fd00:ec2::254]/latest/", "cloud instance metadata"),
    ],
)
def test_a_literal_private_address_is_refused_with_a_named_reason(url: str, reason: str) -> None:
    """The reason is named because an alert saying "refused" teaches nobody
    anything, and "cloud instance metadata address" is a security event while
    "private address" is usually a misconfigured link."""
    with pytest.raises(SsrfRefused, match=reason):
        verify_public_url(url)


def test_an_ipv4_address_mapped_into_ipv6_is_unwrapped_before_checking() -> None:
    """``::ffff:127.0.0.1`` is loopback wearing a public-looking prefix.

    Without the unwrap this passes every IPv6 range check there is.
    """
    with pytest.raises(SsrfRefused, match="loopback"):
        verify_public_url("http://[::ffff:127.0.0.1]/x")
    with pytest.raises(SsrfRefused, match="private"):
        verify_public_url("http://[::ffff:10.0.0.1]/x")


def test_a_6to4_address_is_unwrapped_before_checking() -> None:
    """``2002:7f00:0001::/48`` embeds 127.0.0.1 in a globally-routable prefix."""
    with pytest.raises(SsrfRefused, match="loopback"):
        verify_public_url("http://[2002:7f00:1::]/x")


def test_a_hostname_that_resolves_to_loopback_is_refused(resolves_to: Any) -> None:
    """The central case. Blocking the *string* ``localhost`` blocks nothing:
    an attacker controls their own DNS."""
    resolves_to("127.0.0.1")
    with pytest.raises(SsrfRefused, match="loopback"):
        verify_public_url("https://totally-normal-news.example/article")


def test_an_internal_docker_hostname_is_refused(resolves_to: Any) -> None:
    """``postgres`` resolves inside this deployment's compose network."""
    resolves_to("172.18.0.2")
    with pytest.raises(SsrfRefused, match="private"):
        verify_public_url("http://postgres:5432/")


def test_every_resolved_address_must_pass_not_merely_the_first(resolves_to: Any) -> None:
    """A name with one public A record and one private one would otherwise
    pass the check and then connect to whichever the socket layer preferred."""
    resolves_to("93.184.216.34", "127.0.0.1")
    with pytest.raises(SsrfRefused, match="loopback"):
        verify_public_url("https://mixed.example/a")


def test_a_hostname_that_does_not_resolve_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(SsrfRefused, match="does not resolve"):
        verify_public_url("https://nowhere.invalid/a")


def test_a_public_address_passes_and_reports_what_it_resolved_to(resolves_to: Any) -> None:
    """Returned so a caller that wants to pin the connection to what was
    checked has the addresses, rather than resolving a second time and hoping
    the answer did not change."""
    resolves_to("93.184.216.34")
    check = verify_public_url("https://example.com/article")
    assert check.host == "example.com"
    assert check.port == 443
    assert check.addresses == (ipaddress.ip_address("93.184.216.34"),)


def test_the_default_port_follows_the_scheme(resolves_to: Any) -> None:
    resolves_to("93.184.216.34")
    assert verify_public_url("http://example.com/a").port == 80
    assert verify_public_url("https://example.com/a").port == 443
    assert verify_public_url("https://example.com:8443/a").port == 8443


def test_a_non_default_port_on_a_public_host_is_allowed(resolves_to: Any) -> None:
    """The port is not the control. An attacker who can pick the *address*
    picks the port too, so refusing odd ports would be theatre; refusing
    private addresses is the actual protection."""
    resolves_to("93.184.216.34")
    assert verify_public_url("http://example.com:8080/a").port == 8080


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
def test_credentials_and_query_are_stripped_before_a_url_is_logged() -> None:
    """A URL carrying ``user:password@`` must never reach a log file, and a
    query string is where session tokens live."""
    redacted = redact_url("https://user:hunter2@example.com/path?token=abc123&x=1")
    assert "hunter2" not in redacted
    assert "abc123" not in redacted
    assert "[redacted]@example.com" in redacted
    # The path is kept: without it the line says nothing useful.
    assert redacted.endswith("/path")


def test_a_refusal_message_carries_a_redacted_url() -> None:
    with pytest.raises(SsrfRefused) as caught:
        verify_public_url("https://user:hunter2@127.0.0.1/secret?token=abc123")
    assert "hunter2" not in str(caught.value)
    assert "abc123" not in str(caught.value)


def test_redacting_an_unparseable_url_does_not_raise() -> None:
    assert redact_url("http://[oops") == "(unparseable url)"
