"""Firecrawl v2 thematic web/news discovery, in two stages.

Verified against Firecrawl's current documentation (2026-09-04, billing
re-verified 2026-09-05):

* ``POST https://api.firecrawl.dev/v2/search`` with ``Authorization: Bearer``
* ``POST https://api.firecrawl.dev/v2/scrape`` for one page's content
* **``sources`` is an array of objects**, e.g. ``[{"type": "web"}, {"type":
  "news"}]`` -- not an array of bare strings. This differs from the example in
  StockBrain's specification and is the shape actually accepted.
* ``query`` is capped at 500 characters; **``limit`` is 1-100 *per source***
* search response is ``{"success", "data": {"web": [...], "news": [...],
  "images": [...]}, "creditsUsed", "id", "warning"}``
* **web and news results have different shapes.** Web results carry
  ``description`` (and ``markdown`` when ``scrapeOptions`` is set); news results
  carry ``snippet``, ``date`` and ``imageUrl``. Both are handled explicitly.
* scrape response is ``{"success", "data": {"markdown", "metadata": {...}}}``
  and carries **no** ``creditsUsed``.

Two design decisions here are load-bearing, and both are corrections of the
Phase 2 implementation:

**Search does not scrape.**  ``DiscoveryQuerySpec.scrape_content`` used to
default to ``True``, and the job handler never overrode it, so every broad
thematic search asked Firecrawl to fetch every result page -- 1 credit each,
twenty results a search.  A search now returns metadata only: title, URL,
description or snippet, and date.  That is enough for URL deduplication, for
the deterministic source-category rules and for the cheap classifier to decide
whether the item is worth anything.  Full content is a separate, separately
budgeted call made only for what survives (see
:func:`stockbrain.jobs.handlers.handle_firecrawl_enrich`).

**Nothing here retries.**  Firecrawl documents that "credits are charged
whenever Firecrawl's infrastructure processes a request, even if the target site
returns an HTTP error status code", so a retry is not a free second chance at a
failed call -- it is a second paid call.  ``retry_safe`` is left at its default
``False`` and the caller's durable cooldown is the only thing that tries again.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from dateutil import parser as date_parser
from pydantic import BaseModel, ConfigDict, ValidationError

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.httpclient import ProviderHttpClient, TokenBucket
from stockbrain.ingestion.base import (
    DiscoveryQuerySpec,
    DiscoverySearchOutcome,
    RawSourceDocument,
)
from stockbrain.ingestion.normalizer import classify_source_category
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = [
    "FIRECRAWL_MAX_QUERY_LENGTH",
    "FirecrawlClient",
    "FirecrawlScrapeResult",
    "FirecrawlSearchResult",
]

log = get_logger(__name__)

FIRECRAWL_SEARCH_PATH = "/v2/search"
FIRECRAWL_SCRAPE_PATH = "/v2/scrape"
FIRECRAWL_MAX_QUERY_LENGTH = 500
FIRECRAWL_MAX_LIMIT = 100


class FirecrawlResultMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str | None = None
    sourceURL: str | None = None  # noqa: N815 - provider field name
    statusCode: int | None = None  # noqa: N815 - provider field name
    title: str | None = None
    description: str | None = None


class FirecrawlSearchResult(BaseModel):
    """Union of the web and news result shapes.

    Firecrawl returns different keys per source type; both are optional here and
    :meth:`FirecrawlClient._to_document` picks whichever is populated.
    """

    model_config = ConfigDict(extra="ignore")

    url: str | None = None
    title: str | None = None
    description: str | None = None
    snippet: str | None = None
    markdown: str | None = None
    date: str | None = None
    imageUrl: str | None = None  # noqa: N815 - provider field name
    metadata: FirecrawlResultMetadata | None = None


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
    """Thematic search over the broad web, plus a single-page content fetch.

    Not the primary path for ordinary ticker news -- that is Alpaca's job. This
    finds thematic developments a finance-only feed misses, and is the fallback
    when Alpaca news is unavailable.

    The client knows nothing about the budget.  It is a transport: it builds
    documented request shapes, parses documented response shapes, and reports
    what the provider said it cost.  Deciding whether a call may happen at all
    belongs to :class:`~stockbrain.ingestion.firecrawl_budget.FirecrawlBudget`,
    which needs a database, and a provider adapter that needs a database is a
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
            timeout_seconds=90.0,
            rate_limiter=TokenBucket(rate_per_second=1.0, burst=3),
        )
        self.last_credits_used: int = 0
        """The most recent search's reported cost.  Retained for logging only --
        the authoritative record is a ``firecrawl_calls`` row, because an
        attribute on a client object does not survive a restart and cannot
        refuse anything."""

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------
    def search_sources(self) -> list[str]:
        """The configured ``/v2/search`` source types.

        Each one multiplies the billed result count, which is why it is
        configuration rather than a constant.
        """
        return list(self._settings.firecrawl_search_sources)

    def build_request(self, spec: DiscoveryQuerySpec) -> dict[str, Any]:
        """Build the search request body, in the shape the current API documents."""
        query = spec.query.strip()[:FIRECRAWL_MAX_QUERY_LENGTH]
        body: dict[str, Any] = {
            "query": query,
            # Per source, not per request. Five with two sources is ten billed
            # results, which is exactly one 2-credit block.
            "limit": max(1, min(spec.limit, FIRECRAWL_MAX_LIMIT)),
            # Object form, per current docs. A list of bare strings is the older
            # shape shown in the StockBrain spec and is not what is sent.
            "sources": [{"type": source} for source in self.search_sources()],
            "country": spec.country,
        }
        if spec.freshness:
            body["tbs"] = spec.freshness
        if spec.include_domains:
            body["includeDomains"] = spec.include_domains
        if spec.exclude_domains:
            body["excludeDomains"] = spec.exclude_domains
        if spec.scrape_content:
            # Deliberately opt-in and off by default. Setting this charges one
            # credit for every result the search returns, and it is what turned
            # a 4-credit search into a 24-credit search in Phase 2.
            body["scrapeOptions"] = {
                "formats": [{"type": "markdown"}],
                "onlyMainContent": True,
            }
        return body

    def build_scrape_request(self, url: str) -> dict[str, Any]:
        """Build the single-page scrape body.

        Markdown only.  ``formats`` entries such as ``json`` are documented at
        +4 credits a page and a ``screenshot`` is of no use to a text
        classifier, so the cheapest documented shape is the only one sent.
        """
        return {
            "url": url,
            "formats": [{"type": "markdown"}],
            "onlyMainContent": True,
            "timeout": int(self._settings.firecrawl_scrape_timeout_seconds * 1000),
        }

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------
    async def search(self, spec: DiscoveryQuerySpec) -> DiscoverySearchOutcome:
        """Run one thematic search.  **Never retried** -- see the module docstring."""
        payload = await self._http.request_json(
            "POST",
            FIRECRAWL_SEARCH_PATH,
            json_body=self.build_request(spec),
        )
        return self.parse_response(payload, spec)

    async def scrape(self, url: str) -> FirecrawlScrapeResult:
        """Fetch one page's main content.  **Never retried.**"""
        payload = await self._http.request_json(
            "POST",
            FIRECRAWL_SCRAPE_PATH,
            json_body=self.build_scrape_request(url),
        )
        return self.parse_scrape_response(payload, url)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def parse_response(self, payload: Any, spec: DiscoveryQuerySpec) -> DiscoverySearchOutcome:
        if not isinstance(payload, dict):
            raise ProviderResponseError("firecrawl: response was not a JSON object")
        if payload.get("success") is False:
            raise ProviderResponseError(
                f"firecrawl: search reported failure: {str(payload.get('warning'))[:200]}"
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderResponseError("firecrawl: response had no 'data' object")

        credits_used = payload.get("creditsUsed")
        credits_reported = int(credits_used) if isinstance(credits_used, int | float) else None
        self.last_credits_used = credits_reported or 0

        documents: list[RawSourceDocument] = []
        results_returned = 0
        scraped_pages = 0
        for source_type in ("web", "news", "images"):
            entries = data.get(source_type)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                # Counted before the URL check: a result with no URL is still a
                # result the provider returned and billed for.
                results_returned += 1
                if entry.get("markdown"):
                    scraped_pages += 1
                document = self._to_document(entry, source_type, spec)
                if document is not None:
                    documents.append(document)

        warning = payload.get("warning")
        if warning:
            log.warning("firecrawl_search_warning", warning=str(warning)[:300], query=spec.query)

        METRICS.inc(
            "stockbrain_news_items_received_total",
            len(documents),
            labels={"provider": "firecrawl", "mode": "search"},
        )
        return DiscoverySearchOutcome(
            documents=tuple(documents),
            results_returned=results_returned,
            credits_reported=credits_reported,
            warning=str(warning)[:300] if warning else None,
            scraped_pages=scraped_pages,
        )

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
            # `title` and `description` are documented as string *or* array of
            # strings on this endpoint, unlike on search. Both are collapsed to
            # the first value rather than stringified, so a two-element array
            # does not become "['a', 'b']" in a headline.
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

    def _to_document(
        self, entry: dict[str, Any], source_type: str, spec: DiscoveryQuerySpec
    ) -> RawSourceDocument | None:
        try:
            result = FirecrawlSearchResult.model_validate(entry)
        except ValidationError as exc:
            log.warning(
                "firecrawl_result_schema_mismatch",
                source_type=source_type,
                errors=exc.error_count(),
            )
            return None

        url = result.url or (result.metadata.url if result.metadata else None)
        if not url:
            # Without a URL there is no identity and no provenance; drop it
            # rather than inventing one.
            return None

        # Web results use `description`; news results use `snippet`. Scraped
        # markdown, when present, is the better body for both -- but with the
        # two-stage model it is normally absent, and the snippet is what the
        # classifier triages on.
        summary = result.description or result.snippet
        body = result.markdown or summary

        return RawSourceDocument(
            provider=SourceProvider.FIRECRAWL,
            provider_item_id=None,
            url=url,
            source_name=None,
            source_category=classify_source_category(url),
            headline=result.title or (result.metadata.title if result.metadata else None),
            author=None,
            published_at=_parse_date(result.date),
            updated_at_source=None,
            body=body,
            symbols=[],
            raw_payload=entry,
            metadata={
                "firecrawl_source_type": source_type,
                "discovery_query": spec.query,
                "has_scraped_markdown": result.markdown is not None,
                "summary": summary,
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


def _parse_date(value: str | None) -> dt.datetime | None:
    """Parse Firecrawl's free-form news date, which is not a fixed format."""
    if not value:
        return None
    try:
        parsed = date_parser.parse(value)
    except (ValueError, OverflowError, date_parser.ParserError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
