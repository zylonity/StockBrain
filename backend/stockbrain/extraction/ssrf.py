"""Server-side request forgery protection for outbound page fetches.

Every other outbound request in StockBrain goes to a hostname the operator
configured.  This one does not: the URL comes from a search result, which came
from the open web, which means an attacker who can rank a page can choose an
address this process will connect to.  Inside a Docker network that address
could be ``postgres:5432``, and on a cloud host it could be
``169.254.169.254``.

The rule enforced here is simple and deliberately conservative: **a fetch is
allowed only to a public IP address reached over http or https.**  Everything
else is refused by name.

Three properties matter more than coverage of any particular range:

1. **The check is on the resolved address, not the hostname.**  A name lookup is
   attacker-controlled -- ``evil.example`` can have an A record of ``127.0.0.1``
   -- so blocking the string ``localhost`` blocks nothing.  Every address the
   name resolves to must pass, not just the first.
2. **Redirects are re-checked.**  A public URL that 302s to
   ``http://169.254.169.254/latest/meta-data/`` is the canonical bypass, so
   redirects are followed manually, one hop at a time, with the full check
   applied to each.
3. **Refusals name the reason and never the credential.**  A URL carrying
   userinfo is refused outright *and* redacted before it reaches a log line.

There is no allowlist override and no "internal fetch" mode.  A setting that
turns this off is a setting that will eventually be on.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from stockbrain.logging import get_logger

__all__ = [
    "ALLOWED_SCHEMES",
    "SsrfRefused",
    "UrlCheck",
    "redact_url",
    "resolve_public_addresses",
    "verify_public_url",
]

log = get_logger(__name__)

#: The only two schemes a page fetch may use.  ``file``, ``ftp``, ``gopher``,
#: ``data`` and everything else are refused before any resolution happens --
#: several of them are how a "fetcher" becomes a local file reader.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Cloud instance-metadata endpoints.  These are inside link-local space and are
#: therefore already refused by the range check; they are named separately so
#: that a refusal says *metadata service* rather than *link-local*, because the
#: two mean very different things when you read them in an alert.
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS / Azure / GCP / DO
        ipaddress.ip_address("169.254.170.2"),  # AWS ECS task metadata
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDSv2 over IPv6
    }
)


class SsrfRefused(Exception):
    """A URL was refused before any connection was attempted.

    Not a :class:`~stockbrain.errors.ProviderError`: nothing external failed.
    StockBrain declined to make the request, which is a different fact and must
    not be retried, degraded or reported as a provider outage.
    """

    def __init__(self, reason: str, *, url: str) -> None:
        super().__init__(f"{reason}: {redact_url(url)}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class UrlCheck:
    """A URL that passed, and the addresses it passed on."""

    url: str
    host: str
    port: int
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]


def redact_url(url: str) -> str:
    """Strip credentials and query from a URL before it is logged.

    A URL that somehow carries ``user:password@`` must never reach a log file,
    and a query string is where session tokens live.  The path is kept because
    without it the line says nothing useful.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(unparseable url)"
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    if parts.username or parts.password:
        netloc = f"[redacted]@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _address_refusal(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why this address may not be connected to, or ``None`` if it may.

    Ordered most-specific first so the reason is the informative one.  IPv4
    addresses mapped into IPv6 (``::ffff:127.0.0.1``) are unwrapped first,
    because otherwise a loopback address arrives wearing a public-looking
    prefix.
    """
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _address_refusal(address.ipv4_mapped)
        if address.sixtofour is not None:
            return _address_refusal(address.sixtofour)
        if address.teredo is not None:
            return _address_refusal(address.teredo[1])

    if address in _METADATA_ADDRESSES:
        return "cloud instance metadata address"
    if address.is_loopback:
        return "loopback address"
    if address.is_link_local:
        return "link-local address"
    if address.is_multicast:
        return "multicast address"
    if address.is_reserved:
        return "reserved address"
    if address.is_unspecified:
        return "unspecified address"
    if isinstance(address, ipaddress.IPv6Address) and address.is_site_local:
        return "site-local address"
    if address.is_private:
        # Catches 10/8, 172.16/12, 192.168/16, 100.64/10, fc00::/7 and the
        # documentation ranges. Last because the reasons above are more precise.
        return "private address"
    if not address.is_global:
        return "non-global address"
    return None


def resolve_public_addresses(
    host: str, port: int, *, url: str
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """Resolve ``host`` and refuse unless **every** address is publicly routable.

    Every address, not the first: a name with one public A record and one
    private one would otherwise pass the check and then connect to whichever
    the socket layer preferred.  A literal IP in the URL takes the same path,
    so ``http://127.0.0.1/`` and ``http://localhost/`` are refused identically.
    """
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None

    if literal is not None:
        candidates = [literal]
    else:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise SsrfRefused("hostname does not resolve", url=url) from exc
        candidates = []
        for info in infos:
            sockaddr = info[4]
            try:
                candidates.append(ipaddress.ip_address(sockaddr[0]))
            except ValueError:  # pragma: no cover - getaddrinfo returns literals
                raise SsrfRefused("hostname resolved to an unusable address", url=url) from None

    if not candidates:  # pragma: no cover - getaddrinfo raises rather than returning empty
        raise SsrfRefused("hostname does not resolve", url=url)

    for address in candidates:
        refusal = _address_refusal(address)
        if refusal is not None:
            log.warning(
                "ssrf_refused",
                reason=refusal,
                url=redact_url(url),
                host=host,
            )
            raise SsrfRefused(refusal, url=url)
    return tuple(candidates)


def verify_public_url(url: str) -> UrlCheck:
    """Refuse anything that is not an http(s) URL to a public address.

    Raises :class:`SsrfRefused` with a named reason.  Returns the resolved
    addresses so a caller that wants to pin the connection to what was checked
    has them, rather than having to resolve a second time and hope the answer
    did not change.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise SsrfRefused("URL could not be parsed", url=url) from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SsrfRefused(f"scheme {scheme or '(none)'!r} is not http or https", url=url)
    if parts.username or parts.password:
        # A fetcher must never present credentials it was handed by a web page.
        raise SsrfRefused("URL carries embedded credentials", url=url)

    host = parts.hostname
    if not host:
        raise SsrfRefused("URL has no host", url=url)

    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise SsrfRefused("URL has an invalid port", url=url) from exc

    addresses = resolve_public_addresses(host, port, url=url)
    return UrlCheck(url=url, host=host, port=port, addresses=addresses)
