"""Local article extraction: fetch over plain HTTP, extract with trafilatura.

This is the default extractor and, in normal operation, the only one that runs.
It costs nothing, so the two-stage "search cheaply, read selectively" model
stops being a budget question for the overwhelming majority of pages.

**Library choice.**  ``trafilatura`` 2.2.0 (released 2026-07-31, actively
maintained, Python >= 3.10).  It was chosen over ``readability-lxml`` and a
hand-rolled BeautifulSoup pass because it is the one option that is both
maintained and evaluated against a public benchmark, and because its extra
dependencies are already in this image's tree or are small pure-Python packages:
it needs ``lxml`` (already pinned at 6.1.3 via yfinance) and
``charset_normalizer`` (already pinned at 3.5.1), adding only ``courlan``,
``htmldate``, ``justext`` and ``certifi``.

**Its network stack is deliberately not used.**  ``trafilatura.fetch_url`` has
its own urllib3 downloader with its own redirect handling, and it knows nothing
about SSRF.  Only ``trafilatura.extract`` is called, on bytes this module
fetched itself through the checks in :mod:`stockbrain.extraction.ssrf`.  That
separation is the whole security posture of this module, and it is asserted by a
test.

Everything a fetch touches is untrusted:

* no JavaScript is executed and no browser is driven -- the response is bytes
* redirects are followed **manually**, one hop at a time, with the full SSRF
  check re-applied to every hop, because a public URL redirecting to
  ``169.254.169.254`` is the standard bypass
* the body is read incrementally and abandoned the moment it passes the size
  limit, so a multi-gigabyte response cannot be buffered into memory
* only ``text/html``, ``application/xhtml+xml`` and ``text/plain`` are read
* the declared charset is honoured and undecodable bytes are replaced rather
  than raising
* script, style and template blocks are removed by the extractor, and the
  fallback path removes them explicitly
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import trafilatura
from trafilatura.settings import use_config

from stockbrain.config import Settings
from stockbrain.enums import ExtractionMethod
from stockbrain.extraction.base import ExtractionFailure, ExtractionResult
from stockbrain.extraction.ssrf import SsrfRefused, redact_url, verify_public_url
from stockbrain.ingestion.normalizer import html_to_text
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["ACCEPTABLE_CONTENT_TYPES", "LocalContentExtractor"]

log = get_logger(__name__)

#: Content types worth extracting from.  A PDF, an image or an octet-stream is
#: refused rather than run through an HTML extractor that would return noise.
ACCEPTABLE_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "application/xml", "text/xml", "text/plain"}
)

#: Redirect hops followed before giving up.  Each one is re-checked, so this is
#: a bound on work rather than on trust.
MAX_REDIRECTS = 5


def _extractor_config() -> Any:
    """trafilatura settings tuned for a server, not a crawler.

    The two that matter: its own downloader is never reached because
    :meth:`LocalContentExtractor.extract` never calls it, and extraction is
    given a hard time budget so a pathological document cannot occupy a worker.
    """
    config = use_config()
    # A hard time budget, so a pathological document cannot occupy a worker.
    config.set("DEFAULT", "EXTRACTION_TIMEOUT", "20")
    # Bounds the parsed tree for the same reason. Set through the config rather
    # than as a `max_tree_size` argument: that argument is deprecated in 2.x and
    # passing it raises.
    config.set("DEFAULT", "MAX_TREE_SIZE", "100000")
    config.set("DEFAULT", "MIN_EXTRACTED_SIZE", "250")
    config.set("DEFAULT", "MIN_OUTPUT_SIZE", "250")
    return config


class LocalContentExtractor:
    """Fetches one page over HTTP and extracts its article text locally."""

    name = "local"
    method = ExtractionMethod.LOCAL

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._min_chars = settings.content_extract_min_chars
        self._max_bytes = settings.content_extract_max_bytes
        self._config = _extractor_config()
        # A transport rather than a whole client, so the headers, the timeout
        # and the connection limits below are the ones that are actually used --
        # including under test. Handing in a pre-built client would let a test
        # assert against headers the test itself supplied.
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(settings.content_extract_timeout_seconds),
            # Manual, checked redirects only. httpx would follow them itself and
            # the intermediate hops would never be seen.
            follow_redirects=False,
            headers={
                # An honest, contactable identity. Several publishers block an
                # unidentified client outright, and pretending to be a browser
                # to get around that is both dishonest and fragile.
                "User-Agent": settings.content_extract_user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8",
                "Accept-Language": "en",
            },
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def extract(self, url: str) -> ExtractionResult:
        """Fetch and extract one page.  Never raises for an ordinary failure."""
        try:
            fetched = await self._fetch(url)
        except SsrfRefused as exc:
            METRICS.inc(
                "stockbrain_content_extraction_total",
                labels={"method": "local", "outcome": "refused"},
            )
            return ExtractionResult(
                method=ExtractionMethod.LOCAL,
                url=url,
                failure=ExtractionFailure.REFUSED,
                detail=exc.reason,
            )
        if fetched.failure is not None:
            METRICS.inc(
                "stockbrain_content_extraction_total",
                labels={"method": "local", "outcome": fetched.failure.value.lower()},
            )
            return fetched

        text, title, canonical, published = self._extract_text(
            fetched.metadata["html"], fetched.url
        )
        if not text or len(text) < self._min_chars:
            METRICS.inc(
                "stockbrain_content_extraction_total",
                labels={"method": "local", "outcome": "insufficient_text"},
            )
            return ExtractionResult(
                method=ExtractionMethod.LOCAL,
                url=fetched.url,
                title=title,
                canonical_url=canonical,
                status_code=fetched.status_code,
                content_type=fetched.content_type,
                bytes_read=fetched.bytes_read,
                failure=ExtractionFailure.INSUFFICIENT_TEXT,
                detail=f"extracted {len(text or '')} characters, minimum is {self._min_chars}",
            )

        METRICS.inc(
            "stockbrain_content_extraction_total",
            labels={"method": "local", "outcome": "succeeded"},
        )
        log.info(
            "local_extraction_succeeded",
            url=redact_url(fetched.url),
            characters=len(text),
            bytes_read=fetched.bytes_read,
        )
        return ExtractionResult(
            method=ExtractionMethod.LOCAL,
            url=fetched.url,
            text=text,
            title=title,
            canonical_url=canonical,
            published_at=published,
            status_code=fetched.status_code,
            content_type=fetched.content_type,
            bytes_read=fetched.bytes_read,
        )

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------
    async def _fetch(self, url: str) -> ExtractionResult:
        """Follow up to :data:`MAX_REDIRECTS` hops, checking every one.

        Returns a result carrying the decoded HTML in ``metadata["html"]`` on
        success, or a categorised failure.  Raises only :class:`SsrfRefused`,
        which the caller turns into a refusal it will never pay to retry.
        """
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            # Re-checked on *every* hop. A first URL that passes says nothing
            # about where its Location header points.
            verify_public_url(current)
            try:
                request = self._client.build_request("GET", current)
                response = await self._client.send(request, stream=True)
            except httpx.HTTPError as exc:
                return ExtractionResult(
                    method=ExtractionMethod.LOCAL,
                    url=current,
                    failure=ExtractionFailure.TRANSPORT,
                    detail=type(exc).__name__,
                )

            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return ExtractionResult(
                            method=ExtractionMethod.LOCAL,
                            url=current,
                            status_code=response.status_code,
                            failure=ExtractionFailure.HTTP_ERROR,
                            detail=f"HTTP {response.status_code} with no Location",
                        )
                    # Resolved against the current URL so a relative Location is
                    # handled, and then checked from scratch on the next pass.
                    current = str(httpx.URL(current).join(location))
                    continue

                if not response.is_success:
                    return ExtractionResult(
                        method=ExtractionMethod.LOCAL,
                        url=current,
                        status_code=response.status_code,
                        failure=ExtractionFailure.HTTP_ERROR,
                        detail=f"HTTP {response.status_code}",
                    )

                content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
                if content_type and content_type.lower() not in ACCEPTABLE_CONTENT_TYPES:
                    return ExtractionResult(
                        method=ExtractionMethod.LOCAL,
                        url=current,
                        status_code=response.status_code,
                        content_type=content_type,
                        failure=ExtractionFailure.UNSUPPORTED_CONTENT_TYPE,
                        detail=f"content-type {content_type!r}",
                    )

                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self._max_bytes:
                    return ExtractionResult(
                        method=ExtractionMethod.LOCAL,
                        url=current,
                        status_code=response.status_code,
                        content_type=content_type,
                        failure=ExtractionFailure.TOO_LARGE,
                        detail=f"declared {declared} bytes, limit is {self._max_bytes}",
                    )

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_bytes:
                        # Abandoned mid-stream rather than read and then
                        # measured: a Content-Length header is a claim, and a
                        # chunked response makes no claim at all.
                        return ExtractionResult(
                            method=ExtractionMethod.LOCAL,
                            url=current,
                            status_code=response.status_code,
                            content_type=content_type,
                            bytes_read=len(body),
                            failure=ExtractionFailure.TOO_LARGE,
                            detail=f"body exceeded {self._max_bytes} bytes",
                        )
            except httpx.HTTPError as exc:
                return ExtractionResult(
                    method=ExtractionMethod.LOCAL,
                    url=current,
                    failure=ExtractionFailure.TRANSPORT,
                    detail=type(exc).__name__,
                )
            finally:
                await response.aclose()

            return ExtractionResult(
                method=ExtractionMethod.LOCAL,
                url=current,
                status_code=response.status_code,
                content_type=content_type,
                bytes_read=len(body),
                metadata={"html": _decode(bytes(body), response)},
            )

        return ExtractionResult(
            method=ExtractionMethod.LOCAL,
            url=current,
            failure=ExtractionFailure.HTTP_ERROR,
            detail=f"more than {MAX_REDIRECTS} redirects",
        )

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    def _extract_text(
        self, html: str, url: str
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Article text, title, canonical URL and date, from already-fetched HTML.

        ``trafilatura`` is given the string, never the URL, so there is no path
        by which it could fetch anything.  When it declines -- which it does for
        pages that are genuinely not articles -- the deterministic
        :func:`~stockbrain.ingestion.normalizer.html_to_text` stripper is the
        fallback, and its output still has to clear the minimum length.
        """
        title: str | None = None
        canonical: str | None = None
        published: str | None = None
        text: str | None = None

        try:
            document = trafilatura.bare_extraction(
                html,
                url=url,
                config=self._config,
                # No comment sections: they are user-generated text on an
                # untrusted page heading for an LLM prompt.
                include_comments=False,
                include_tables=True,
                include_images=False,
                include_links=False,
                favor_precision=True,
                with_metadata=True,
            )
        except Exception as exc:  # trafilatura raises broadly on malformed input
            log.info(
                "local_extraction_library_failed",
                url=redact_url(url),
                error_type=type(exc).__name__,
            )
            document = None

        if document is not None:
            text = _document_field(document, "text")
            title = _document_field(document, "title")
            canonical = _document_field(document, "url")
            published = _document_field(document, "date")

        if not text:
            # Deterministic fallback. Strips script/style/noscript outright, so
            # a page trafilatura rejected still cannot contribute JavaScript
            # source to a classifier prompt.
            text = html_to_text(html) or None

        return (
            text.strip() if text else None,
            title.strip() if title else None,
            canonical.strip() if canonical else None,
            _iso_or_none(published),
        )


def _document_field(document: Any, name: str) -> str | None:
    value = getattr(document, name, None)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _iso_or_none(value: str | None) -> str | None:
    """Keep a date only if it parses.  A string that does not is not a date."""
    if not value:
        return None
    try:
        dt.date.fromisoformat(value[:10])
    except ValueError:
        return None
    return value


def _decode(body: bytes, response: httpx.Response) -> str:
    """Decode using the declared charset, replacing what will not decode.

    Replacement rather than an exception: a single bad byte in a long article is
    not a reason to discard the article, and the alternative -- guessing a
    different codec -- silently changes the text that gets hashed.
    """
    encoding = response.charset_encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        # The page declared a charset Python has never heard of.
        return body.decode("utf-8", errors="replace")
