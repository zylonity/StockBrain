"""Firecrawl as a fallback extractor, and nothing else.

Firecrawl was StockBrain's primary discovery provider through Phase 9.  It is
not any more: Brave does routine thematic search and Exa does semantic search,
both at a fraction of the price, and neither fetches a page as a side effect of
searching.  What Firecrawl is still good at is the case local extraction cannot
handle -- a publisher that answers 403 to a plain HTTP client, or serves an
empty shell that needs rendering.

That case is rare, so this adapter is rare.  Its contract:

* it is reached **only** after :class:`~stockbrain.extraction.local.LocalContentExtractor`
  failed, and only for the failure categories a different fetcher could
  plausibly fix (:attr:`~stockbrain.extraction.base.ExtractionFailure.fallback_eligible`)
* it is reached only for a URL whose event the classifier already promoted
* **one attempt per URL, ever.**  Not one per job, not one per day: the source
  row records that the attempt happened, and a second job for the same source
  does not call this again
* **nothing retries it.**  Firecrawl documents that "credits are charged
  whenever Firecrawl's infrastructure processes a request, even if the target
  site returns an HTTP error status code", so a retry is a second charge on a
  call that already failed

Verified against <https://docs.firecrawl.dev/api-reference/endpoint/scrape> and
``/billing`` on **2026-09-05**: ``POST https://api.firecrawl.dev/v2/scrape``,
``Authorization: Bearer``, response ``{"success", "data": {"markdown",
"metadata": {"title", "description", "url", "sourceURL", "statusCode",
"contentType", "language"}}}`` with **no** ``creditsUsed`` field; a plain
markdown scrape is 1 credit per page, and ``json`` (+4), prompt-injection
checking (+4) and zero-data-retention (+1) are extra per page and are not
requested.
"""

from __future__ import annotations

from stockbrain.enums import ExtractionMethod
from stockbrain.errors import ProviderError
from stockbrain.extraction.base import ExtractionFailure, ExtractionResult
from stockbrain.extraction.ssrf import SsrfRefused, redact_url, verify_public_url
from stockbrain.ingestion.firecrawl import FirecrawlClient
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["FirecrawlContentExtractor"]

log = get_logger(__name__)


class FirecrawlContentExtractor:
    """One paid page fetch, through Firecrawl's ``/v2/scrape``.

    Knows nothing about the budget.  Whether this call may happen at all is
    decided by :class:`~stockbrain.ingestion.provider_budget.ProviderCallBudget`
    before the adapter is touched, for the same reason the transport clients
    never own their own accounting.
    """

    name = "firecrawl"
    method = ExtractionMethod.FIRECRAWL

    def __init__(self, client: FirecrawlClient) -> None:
        self._client = client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def extract(self, url: str, *, language: str = "en") -> ExtractionResult:
        """Scrape one page.  **Never retried.**

        ``language`` is accepted to satisfy the extractor interface but is not
        sent: Firecrawl is the paid fallback, and the free local extractor is
        the one that honours a source's language.  The SSRF check runs here too.
        Firecrawl fetches from its own infrastructure rather than from inside
        this network, so it is not an SSRF path in the usual sense -- but asking
        a paid third party to fetch ``http://postgres:5432`` is still a request
        StockBrain should never make, and it would still be billed.
        """
        try:
            verify_public_url(url)
        except SsrfRefused as exc:
            METRICS.inc(
                "stockbrain_content_extraction_total",
                labels={"method": "firecrawl", "outcome": "refused"},
            )
            return ExtractionResult(
                method=ExtractionMethod.FIRECRAWL,
                url=url,
                failure=ExtractionFailure.REFUSED,
                detail=exc.reason,
            )

        try:
            scraped = await self._client.scrape(url)
        except ProviderError as exc:
            METRICS.inc(
                "stockbrain_content_extraction_total",
                labels={"method": "firecrawl", "outcome": "provider_error"},
            )
            log.warning(
                "firecrawl_fallback_failed",
                url=redact_url(url),
                error_type=type(exc).__name__,
            )
            return ExtractionResult(
                method=ExtractionMethod.FIRECRAWL,
                url=url,
                status_code=getattr(exc, "status_code", None),
                failure=ExtractionFailure.TRANSPORT,
                # The class name, never the provider's body: a Firecrawl error
                # body can echo the request, and the request carries the key.
                detail=type(exc).__name__,
            )

        if not scraped.has_content:
            METRICS.inc(
                "stockbrain_content_extraction_total",
                labels={"method": "firecrawl", "outcome": "insufficient_text"},
            )
            return ExtractionResult(
                method=ExtractionMethod.FIRECRAWL,
                url=scraped.url,
                title=scraped.title,
                status_code=scraped.status_code,
                failure=ExtractionFailure.INSUFFICIENT_TEXT,
                detail="scrape returned no markdown",
            )

        METRICS.inc(
            "stockbrain_content_extraction_total",
            labels={"method": "firecrawl", "outcome": "succeeded"},
        )
        return ExtractionResult(
            method=ExtractionMethod.FIRECRAWL,
            url=scraped.url,
            text=scraped.markdown,
            title=scraped.title,
            status_code=scraped.status_code,
            content_type=str(scraped.metadata.get("contentType") or "") or None,
            bytes_read=len(scraped.markdown or ""),
            metadata=dict(scraped.metadata),
        )
