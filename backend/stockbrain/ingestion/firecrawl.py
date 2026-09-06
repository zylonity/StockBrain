"""Firecrawl transport: one page's content, and nothing else.

``/v2/search`` used to live here.  It is gone -- not disabled behind a flag,
removed -- because leaving a paid primary-discovery path in the tree that
nothing schedules is how it gets scheduled again.  Brave and Exa do discovery
now (:mod:`stockbrain.ingestion.brave`, :mod:`stockbrain.ingestion.exa`), and
the rows that Firecrawl search discovered between Phase 2 and Phase 9 are still
in the database with ``provider = 'FIRECRAWL'``, still valid, and deliberately
not rewritten.

What remains is ``/v2/scrape``, used as the fallback extractor behind
:class:`~stockbrain.extraction.firecrawl.FirecrawlContentExtractor`.

Verified against Firecrawl's current documentation, **2026-09-05**
(<https://docs.firecrawl.dev/api-reference/endpoint/scrape>, ``/billing``):

* ``POST https://api.firecrawl.dev/v2/scrape`` with ``Authorization: Bearer``
* body ``{"url", "formats", "onlyMainContent", "timeout"}``; ``timeout`` is
  milliseconds, 1000-300000
* response ``{"success", "data": {"markdown", "metadata": {...}}}`` and it
  carries **no** ``creditsUsed``, so the reservation is the charge
* ``title`` and ``description`` are documented as string *or* array of strings
  on this endpoint, unlike on search; both are collapsed to the first value
  rather than stringified, so a two-element array does not become
  ``"['a', 'b']"`` in a headline
* a plain markdown scrape is **1 credit per page**; ``json`` (+4),
  prompt-injection checking (+4), zero-data-retention (+1) and PDF parsing
  (+1/page) are extra and none is requested
* "credits are charged whenever Firecrawl's infrastructure processes a request,
  even if the target site returns an HTTP error status code" -- so a retry is
  not a free second chance, it is a second paid call.  ``retry_safe`` is left at
  its default ``False`` and nothing above this layer retries either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from stockbrain.config import Settings
from stockbrain.errors import ProviderResponseError
from stockbrain.httpclient import ProviderHttpClient, TokenBucket
from stockbrain.logging import get_logger

__all__ = [
    "FIRECRAWL_SCRAPE_PATH",
    "FirecrawlClient",
    "FirecrawlScrapeResult",
]

log = get_logger(__name__)

FIRECRAWL_SCRAPE_PATH = "/v2/scrape"


@dataclass(frozen=True, slots=True)
class FirecrawlScrapeResult:
    """One page's fetched content."""

    url: str
    markdown: str | None
    title: str | None = None
    description: str | None = None
    status_code: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def has_content(self) -> bool:
        return bool(self.markdown and self.markdown.strip())


class FirecrawlClient:
    """A single-page content fetch.

    The client knows nothing about the budget.  It is a transport: it builds a
    documented request shape and parses a documented response shape.  Deciding
    whether a call may happen at all belongs to
    :class:`~stockbrain.ingestion.provider_budget.ProviderCallBudget`, which
    needs a database -- and a provider adapter that needs a database is a
    provider adapter that cannot be unit-tested against a recorded payload.
    """

    name = "firecrawl"

    def __init__(self, settings: Settings, *, http: ProviderHttpClient | None = None) -> None:
        self._settings = settings
        self._http = http or ProviderHttpClient(
            provider="firecrawl",
            base_url=settings.firecrawl_base_url,
            headers={
                "Authorization": f"Bearer {settings.firecrawl_api_key.get_secret_value()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout_seconds=settings.firecrawl_scrape_timeout_seconds + 30.0,
            rate_limiter=TokenBucket(rate_per_second=1.0, burst=2),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def build_scrape_request(self, url: str) -> dict[str, Any]:
        """Build the single-page scrape body.

        Markdown only.  A ``json`` format is documented at +4 credits a page and
        a screenshot is of no use to a text classifier, so the cheapest
        documented shape is the only one sent.
        """
        return {
            "url": url,
            "formats": [{"type": "markdown"}],
            "onlyMainContent": True,
            "timeout": int(self._settings.firecrawl_scrape_timeout_seconds * 1000),
        }

    async def scrape(self, url: str) -> FirecrawlScrapeResult:
        """Fetch one page's main content.  **Never retried.**"""
        payload = await self._http.request_json(
            "POST",
            FIRECRAWL_SCRAPE_PATH,
            json_body=self.build_scrape_request(url),
        )
        return self.parse_scrape_response(payload, url)

    def parse_scrape_response(self, payload: Any, url: str) -> FirecrawlScrapeResult:
        if not isinstance(payload, dict):
            raise ProviderResponseError("firecrawl: scrape response was not a JSON object")
        if payload.get("success") is False:
            raise ProviderResponseError(
                f"firecrawl: scrape reported failure: {str(payload.get('error'))[:200]}"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderResponseError("firecrawl: scrape response had no 'data' object")

        markdown = data.get("markdown")
        raw_metadata = data.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        return FirecrawlScrapeResult(
            url=_first_string(metadata.get("sourceURL"))
            or _first_string(metadata.get("url"))
            or url,
            markdown=markdown if isinstance(markdown, str) else None,
            title=_first_string(metadata.get("title")),
            description=_first_string(metadata.get("description")),
            status_code=(
                int(metadata["statusCode"]) if isinstance(metadata.get("statusCode"), int) else None
            ),
            metadata={
                key: value
                for key, value in metadata.items()
                if key in {"sourceURL", "url", "statusCode", "contentType", "language"}
            },
        )


def _first_string(value: Any) -> str | None:
    """Collapse a documented ``string | string[]`` field to one string."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                return item
    return None
