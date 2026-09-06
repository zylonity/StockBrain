"""Local article extraction: fetching, limits, decoding and redirect safety.

The extractor is the one component in StockBrain that connects to an address a
stranger chose, so most of what is asserted here is a refusal rather than a
capability.

Every test drives a real :class:`httpx.AsyncClient` over a mock transport.  The
alternative -- mocking the extractor's own fetch -- would assert only that the
test's mock was called, and the behaviour under test *is* the fetch: its size
limit, its content-type check, its redirect handling and its decoding.
"""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest

from stockbrain.config import Settings
from stockbrain.enums import ExtractionMethod
from stockbrain.extraction.base import ExtractionFailure
from stockbrain.extraction.local import ACCEPTABLE_CONTENT_TYPES, LocalContentExtractor

ARTICLE_HTML = """
<!doctype html>
<html><head>
  <title>Utility orders 400 transformers</title>
  <link rel="canonical" href="https://example.com/canonical-article">
  <meta property="article:published_time" content="2026-09-04T11:02:00Z">
  <style>.ad { display: none }</style>
  <script>window.__DATA__ = {"secret": "should never be extracted"};</script>
</head><body>
  <nav>Home About Contact</nav>
  <article>
    <h1>Utility orders 400 transformers</h1>
    <p>A regional utility has placed an order for four hundred large power
    transformers, the largest single order the manufacturer has recorded, as
    interconnection queues lengthen across the region.</p>
    <p>Delivery is scheduled across the next thirty months. The manufacturer
    said its order book is now full through the end of the decade and that it
    is evaluating a second production line to meet demand it had not forecast.</p>
    <p>Analysts said the order is evidence that grid constraints have become the
    binding limit on data centre construction rather than chip supply, a shift
    that has been visible in procurement data for several quarters.</p>
  </article>
  <footer>Copyright</footer>
</body></html>
"""


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every hostname in this module resolves to a public address.

    SSRF refusal has its own module; pinning it here keeps these tests about
    fetching and extraction rather than about DNS.
    """

    def fake_getaddrinfo(host: str, port: int, *args: object, **kwargs: object) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _extractor(handler: Any, **overrides: object) -> LocalContentExtractor:
    """The real extractor, with only its transport replaced.

    Only the transport: the headers, timeout, redirect policy and connection
    limits are the ones the extractor builds for itself, so a test can assert
    against them meaningfully.
    """
    return LocalContentExtractor(_settings(**overrides), transport=httpx.MockTransport(handler))


def _html(body: str = ARTICLE_HTML, **kwargs: Any) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/html"}, **kwargs)

    return handler


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
async def test_an_article_is_extracted_to_clean_text() -> None:
    extractor = _extractor(_html())
    result = await extractor.extract("https://example.com/article")
    await extractor.aclose()

    assert result.succeeded
    assert result.method is ExtractionMethod.LOCAL
    assert result.failure is None
    assert "four hundred large power transformers" in (result.text or "")
    assert result.status_code == 200
    assert result.bytes_read > 0


async def test_script_and_style_contents_never_reach_the_extracted_text() -> None:
    """Untrusted JavaScript heading for an LLM prompt is the concern.

    The extractor strips it, and so does the deterministic fallback, so there is
    no path by which a page's script body becomes classifier input.
    """
    extractor = _extractor(_html())
    result = await extractor.extract("https://example.com/article")
    await extractor.aclose()

    text = result.text or ""
    assert "should never be extracted" not in text
    assert "window.__DATA__" not in text
    assert "display: none" not in text


async def test_boilerplate_is_dropped_in_favour_of_the_article() -> None:
    """Navigation and footers are not evidence of anything."""
    extractor = _extractor(_html())
    result = await extractor.extract("https://example.com/article")
    await extractor.aclose()
    assert "Home About Contact" not in (result.text or "")


async def test_the_page_canonical_url_is_reported_when_it_declares_one() -> None:
    extractor = _extractor(_html())
    result = await extractor.extract("https://example.com/article?utm_source=x")
    await extractor.aclose()
    assert result.canonical_url == "https://example.com/canonical-article"


async def test_malformed_html_still_yields_text_rather_than_raising() -> None:
    """A publisher's broken markup is not an exceptional condition."""
    broken = "<html><body><article><p>" + ("Grid capacity is the binding constraint. " * 30)
    extractor = _extractor(_html(broken))
    result = await extractor.extract("https://example.com/broken")
    await extractor.aclose()
    assert result.succeeded
    assert "binding constraint" in (result.text or "")


# ---------------------------------------------------------------------------
# Refusals and failures -- each categorised, because the category decides
# whether a *paid* fallback is worth attempting
# ---------------------------------------------------------------------------
async def test_a_private_address_is_refused_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And ``REFUSED`` is never fallback-eligible: asking a paid provider to
    fetch the same forbidden target would spend money on the same refusal."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=ARTICLE_HTML)

    def loopback(host: str, port: int, *args: object, **kwargs: object) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", loopback)
    extractor = _extractor(handler)
    result = await extractor.extract("https://evil.example/a")
    await extractor.aclose()

    assert calls == 0
    assert result.failure is ExtractionFailure.REFUSED
    assert not result.failure.fallback_eligible


async def test_a_non_html_content_type_is_refused() -> None:
    """A PDF run through an HTML extractor returns noise, and a second fetcher
    returns the same bytes -- so this is not fallback-eligible either."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})

    extractor = _extractor(handler)
    result = await extractor.extract("https://example.com/a.pdf")
    await extractor.aclose()

    assert result.failure is ExtractionFailure.UNSUPPORTED_CONTENT_TYPE
    assert not result.failure.fallback_eligible
    assert result.content_type == "application/pdf"


def test_the_accepted_content_types_are_text_only() -> None:
    assert "application/pdf" not in ACCEPTABLE_CONTENT_TYPES
    assert "image/png" not in ACCEPTABLE_CONTENT_TYPES
    assert "text/html" in ACCEPTABLE_CONTENT_TYPES


async def test_a_declared_oversize_body_is_refused_before_it_is_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="x",
            headers={"content-type": "text/html", "content-length": "99999999"},
        )

    extractor = _extractor(handler, content_extract_max_bytes=10_000)
    result = await extractor.extract("https://example.com/huge")
    await extractor.aclose()
    assert result.failure is ExtractionFailure.TOO_LARGE


async def test_an_undeclared_oversize_body_is_abandoned_mid_stream() -> None:
    """A ``Content-Length`` header is a claim, and a chunked response makes no
    claim at all -- so the limit is enforced while reading, not from the header.

    The response here is a generator, which is exactly the shape that carries no
    length: without the streaming check a publisher could hand this process a
    gigabyte and it would be buffered into memory before anyone measured it.
    """
    chunks_sent = 0

    async def body() -> Any:
        nonlocal chunks_sent
        for _ in range(200):
            chunks_sent += 1
            yield b"a" * 10_000

    def handler(request: httpx.Request) -> httpx.Response:
        # An async iterator, which is the shape that carries no Content-Length.
        return httpx.Response(200, content=body(), headers={"content-type": "text/html"})

    extractor = _extractor(handler, content_extract_max_bytes=50_000)
    result = await extractor.extract("https://example.com/huge")
    await extractor.aclose()

    assert result.failure is ExtractionFailure.TOO_LARGE
    assert result.bytes_read > 50_000
    # Abandoned rather than drained: only a handful of the two hundred chunks
    # were ever pulled.
    assert chunks_sent < 20


async def test_an_http_error_is_categorised_as_fallback_eligible() -> None:
    """A 403 to a plain client is exactly the case a rendering scraper exists
    for, so this is one of the three categories a paid retry may address."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    extractor = _extractor(handler)
    result = await extractor.extract("https://example.com/paywalled")
    await extractor.aclose()

    assert result.failure is ExtractionFailure.HTTP_ERROR
    assert result.failure.fallback_eligible
    assert result.status_code == 403


async def test_a_timeout_is_categorised_as_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    extractor = _extractor(handler)
    result = await extractor.extract("https://example.com/slow")
    await extractor.aclose()

    assert result.failure is ExtractionFailure.TRANSPORT
    assert result.failure.fallback_eligible
    # The exception class name, never its message: a transport error can carry
    # the full request URL.
    assert result.detail == "ReadTimeout"


async def test_a_javascript_shell_is_insufficient_text_not_success() -> None:
    """A consent wall, a paywall stub and an empty SPA all return 200 with a
    few dozen words.  Treating that as an article would mark the URL read while
    putting nothing useful in front of the classifier."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><body><div id='root'></div></body></html>",
            headers={"content-type": "text/html"},
        )

    extractor = _extractor(handler)
    result = await extractor.extract("https://example.com/spa")
    await extractor.aclose()

    assert result.failure is ExtractionFailure.INSUFFICIENT_TEXT
    assert result.failure.fallback_eligible


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------
async def test_a_redirect_to_another_public_page_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/short":
            return httpx.Response(302, headers={"location": "https://example.com/article"})
        return httpx.Response(200, text=ARTICLE_HTML, headers={"content-type": "text/html"})

    extractor = _extractor(handler)
    result = await extractor.extract("https://example.com/short")
    await extractor.aclose()

    assert result.succeeded
    # The URL actually read, not the one asked for.
    assert result.url == "https://example.com/article"


async def test_a_relative_redirect_is_resolved_against_the_current_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/short":
            return httpx.Response(302, headers={"location": "/article"})
        return httpx.Response(200, text=ARTICLE_HTML, headers={"content-type": "text/html"})

    extractor = _extractor(handler)
    result = await extractor.extract("https://example.com/short")
    await extractor.aclose()
    assert result.url == "https://example.com/article"


async def test_a_redirect_from_public_to_private_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The canonical SSRF bypass.**

    A URL that passes the check and then 302s to ``169.254.169.254`` is how a
    fetcher reaches a cloud metadata service.  Every hop is re-checked, so the
    second address is refused even though the first passed.
    """
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    def resolver(host: str, port: int, *args: object, **kwargs: object) -> list[Any]:
        address = "93.184.216.34" if host == "example.com" else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    extractor = _extractor(handler)
    result = await extractor.extract("https://example.com/redirector")
    await extractor.aclose()

    assert result.failure is ExtractionFailure.REFUSED
    assert "metadata" in (result.detail or "")
    # The first hop was fetched; the second was never requested.
    assert requested == ["https://example.com/redirector"]


async def test_a_redirect_loop_terminates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/loop"})

    extractor = _extractor(handler)
    result = await extractor.extract("https://example.com/loop")
    await extractor.aclose()
    assert result.failure is ExtractionFailure.HTTP_ERROR
    assert "redirects" in (result.detail or "")


async def test_a_redirect_without_a_location_is_an_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    extractor = _extractor(handler)
    result = await extractor.extract("https://example.com/broken-redirect")
    await extractor.aclose()
    assert result.failure is ExtractionFailure.HTTP_ERROR


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------
async def test_a_declared_charset_is_honoured() -> None:
    body = ARTICLE_HTML.replace("A regional utility", "Ein regionaler Versorger über")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body.encode("iso-8859-1"),
            headers={"content-type": "text/html; charset=iso-8859-1"},
        )

    extractor = _extractor(handler)
    result = await extractor.extract("https://example.com/de")
    await extractor.aclose()
    assert "über" in (result.text or "")


async def test_undecodable_bytes_are_replaced_rather_than_raising() -> None:
    """A single bad byte in a long article is not a reason to discard the
    article, and guessing a different codec silently changes what gets hashed.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=ARTICLE_HTML.encode("utf-8") + b"\xff\xfe invalid",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    extractor = _extractor(handler)
    result = await extractor.extract("https://example.com/mixed")
    await extractor.aclose()
    assert result.succeeded


async def test_an_unknown_declared_charset_falls_back_to_utf8() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=ARTICLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html; charset=x-nonexistent-codec"},
        )

    extractor = _extractor(handler)
    result = await extractor.extract("https://example.com/weird-charset")
    await extractor.aclose()
    assert result.succeeded


# ---------------------------------------------------------------------------
# Capability boundaries
# ---------------------------------------------------------------------------
async def test_the_extractor_identifies_itself_honestly() -> None:
    """Impersonating a browser to get past a publisher's block is dishonest and
    fragile; naming the agent and making it contactable is neither."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, text=ARTICLE_HTML, headers={"content-type": "text/html"})

    extractor = _extractor(handler)
    await extractor.extract("https://example.com/article")
    await extractor.aclose()

    assert "StockBrain" in seen["user-agent"]
    assert "Mozilla" not in seen["user-agent"]


def test_the_extractor_never_uses_trafilaturas_own_downloader() -> None:
    """trafilatura ships a urllib3 downloader with its own redirect handling
    and no idea what SSRF is.  Only ``bare_extraction`` is called, on bytes this
    module fetched through its own checks -- that separation is the security
    posture of the whole module."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[2] / "stockbrain" / "extraction" / "local.py"
    ).read_text()
    # Call sites, not prose: the module docstring names `trafilatura.fetch_url`
    # precisely to say it is not used, and a substring match would trip on that.
    for forbidden in ("fetch_url(", "fetch_response(", "from trafilatura.downloads"):
        assert forbidden not in source, f"local.py calls {forbidden!r}"
    assert "bare_extraction(" in source


def test_no_extraction_module_can_execute_javascript_or_a_shell() -> None:
    """No browser automation, no subprocess, no eval.  A page is bytes."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "stockbrain" / "extraction"
    for path in sorted(root.glob("*.py")):
        source = path.read_text()
        for forbidden in (
            "import subprocess",
            "import os.system",
            "playwright",
            "selenium",
            "pyppeteer",
            "eval(",
            "exec(",
            "__import__",
        ):
            assert forbidden not in source, f"{path.name} contains {forbidden!r}"


def test_no_extraction_module_can_reach_the_broker_risk_or_control_state() -> None:
    """An extractor's only capability is one outbound HTTP request.

    It has no path to a broker client, a risk engine, the proposal service or
    the durable kill switch -- which is what makes "untrusted page content
    reaches an extractor" a bounded statement rather than an open one.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "stockbrain" / "extraction"
    forbidden = (
        "stockbrain.broker",
        "stockbrain.risk",
        "stockbrain.execution",
        "stockbrain.proposals",
        "stockbrain.control",
        "stockbrain.telegram",
        "stockbrain.db",
    )
    for path in sorted(root.glob("*.py")):
        imports = "\n".join(
            line
            for line in path.read_text().splitlines()
            if line.lstrip().startswith(("import ", "from "))
        )
        for module in forbidden:
            assert module not in imports, f"{path.name} imports {module}"


def test_every_outbound_request_is_preceded_by_the_ssrf_check() -> None:
    """The check is not advisory and there is no second network path.

    Asserted structurally because a future edit that adds a fetch *next to* the
    check rather than after it would pass every behavioural test in this file:
    the tests drive the one call site that exists, and this asserts it is still
    the only one.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "stockbrain" / "extraction"

    # The local extractor owns an httpx client. Everything it is allowed to ask
    # that client for; `send` is the only one that opens a socket.
    local = (root / "local.py").read_text()
    permitted = {"build_request", "send", "aclose"}
    for node in ast.walk(ast.parse(local)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = ast.get_source_segment(local, node.func.value) or ""
        if receiver.endswith("_client"):
            assert node.func.attr in permitted, f"unexpected client call .{node.func.attr}()"

    # And that call verifies first, on **every hop** -- the verification is
    # inside the redirect loop, not before it. A public URL that 302s to a
    # metadata service is the bypass this ordering refuses.
    loop_at = local.index("for _hop in range(MAX_REDIRECTS + 1):")
    verify_at = local.index("verify_public_url(current)")
    send_at = local.index("await self._client.send(")
    assert loop_at < verify_at < send_at, "local.py sends before it verifies"

    # The paid fallback hands a URL to a third party rather than connecting
    # itself, but asking Firecrawl to fetch `http://postgres:5432` is still a
    # request StockBrain should never make -- and would still be billed.
    firecrawl = (root / "firecrawl.py").read_text()
    assert firecrawl.index("verify_public_url(url)") < firecrawl.index(
        "await self._client.scrape("
    ), "the Firecrawl fallback scrapes before it verifies"
