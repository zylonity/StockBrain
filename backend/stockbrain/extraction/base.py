"""The content-extraction interface.

Search and extraction are two capabilities, not one.  Firecrawl offered both
behind one credential and one credit balance, and the consequence was that
turning on discovery turned on page fetching -- at one credit per result, for
pages nothing had yet judged worth reading.  Keeping the interfaces apart is
what makes "search everything cheaply, read almost nothing" expressible.

An extractor is given a URL and returns text or an explained failure.  It is
never given a document, an event or a classification: what to read is the
caller's decision and the caller's budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from stockbrain.enums import ExtractionMethod

__all__ = [
    "ContentExtractor",
    "ExtractionFailure",
    "ExtractionResult",
]


class ExtractionFailure(StrEnum):
    """Why an extraction produced nothing usable.

    A name rather than a sentence, because the caller has to make a decision
    from it: only some of these are worth spending a paid fallback on.  A page
    that was refused for pointing at a private address is not going to become
    extractable, and a PDF is not going to become HTML.
    """

    REFUSED = "REFUSED"
    """Refused before any connection: bad scheme, private address, credentials
    in the URL.  **Never** eligible for a paid fallback -- the paid provider
    would be asked to fetch the same forbidden target."""

    UNSUPPORTED_CONTENT_TYPE = "UNSUPPORTED_CONTENT_TYPE"
    """The response was not HTML or plain text.  Not eligible: a fallback
    scraper returns the same bytes."""

    TOO_LARGE = "TOO_LARGE"
    HTTP_ERROR = "HTTP_ERROR"
    """A complete response with a non-2xx status.  Eligible: a 403 to a plain
    client is exactly the case a rendering scraper exists for."""

    TRANSPORT = "TRANSPORT"
    """Timeout, connection reset, TLS failure.  Eligible."""

    INSUFFICIENT_TEXT = "INSUFFICIENT_TEXT"
    """A 200 that yielded too little text to be an article -- a JavaScript
    shell, a consent wall, a paywall stub.  Eligible, and the main reason the
    fallback exists at all."""

    @property
    def fallback_eligible(self) -> bool:
        """Whether a *paid* extractor could plausibly do better.

        Deliberately restrictive.  Spending on a failure a second provider will
        reproduce is how a fallback becomes a cost storm.
        """
        return self in {
            ExtractionFailure.HTTP_ERROR,
            ExtractionFailure.TRANSPORT,
            ExtractionFailure.INSUFFICIENT_TEXT,
        }


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """What one extraction attempt produced.

    Always returned, never raised.  A failed extraction is an ordinary outcome
    with a category attached -- the caller has to record it either way, so that
    the same URL is not paid for twice.
    """

    method: ExtractionMethod
    url: str
    """The URL actually read, after redirects.  Not the URL asked for."""

    text: str | None = None
    title: str | None = None
    canonical_url: str | None = None
    """The page's own ``<link rel=canonical>``, when it declares one."""

    published_at: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    bytes_read: int = 0
    failure: ExtractionFailure | None = None
    detail: str | None = None
    """A short, credential-free explanation.  Never a provider body."""

    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.failure is None and bool(self.text and self.text.strip())


@runtime_checkable
class ContentExtractor(Protocol):
    """Turns one URL into article text.

    Implementations must never execute JavaScript, never drive a browser, never
    shell out and never read a local path.  The only capability an extractor
    has is an outbound HTTP request to a public address.
    """

    name: str
    method: ExtractionMethod

    async def extract(self, url: str) -> ExtractionResult: ...

    async def aclose(self) -> None: ...
