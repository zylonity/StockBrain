"""Firecrawl v2 thematic web/news discovery.

Verified against Firecrawl's current documentation (2026-09-04):

* ``POST https://api.firecrawl.dev/v2/search`` with ``Authorization: Bearer``
* **``sources`` is an array of objects**, e.g. ``[{"type": "web"}, {"type":
  "news"}]`` -- not an array of bare strings. This differs from the example in
  StockBrain's specification and is the shape actually accepted.
* ``query`` is capped at 500 characters; ``limit`` is 1-100 per source
* response is ``{"success", "data": {"web": [...], "news": [...], "images":
  [...]}, "creditsUsed", "id", "warning"}``
* **web and news results have different shapes.** Web results carry
  ``description`` (and ``markdown`` when ``scrapeOptions`` is set); news results
  carry ``snippet``, ``date`` and ``imageUrl``. Both are handled explicitly.

Search is a read operation with no side effect other than credit consumption, so
it is safe to retry a request that clearly failed before completion. Credits are
recorded per query for cost control.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from dateutil import parser as date_parser
from pydantic import BaseModel, ConfigDict, ValidationError

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.httpclient import ProviderHttpClient, TokenBucket
from stockbrain.ingestion.base import DiscoveryQuerySpec, RawSourceDocument
from stockbrain.ingestion.normalizer import classify_source_category
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["FIRECRAWL_MAX_QUERY_LENGTH", "FirecrawlClient", "FirecrawlSearchResult"]

log = get_logger(__name__)

FIRECRAWL_SEARCH_PATH = "/v2/search"
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


class FirecrawlClient:
    """Thematic search over the broad web.

    Not the primary path for ordinary ticker news -- that is Alpaca's job. This
    finds thematic developments a finance-only feed misses, and is the fallback
    when Alpaca news is unavailable.
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

    async def aclose(self) -> None:
        await self._http.aclose()

    def build_request(self, spec: DiscoveryQuerySpec) -> dict[str, Any]:
        """Build the request body, in the shape the current API documents."""
        query = spec.query.strip()[:FIRECRAWL_MAX_QUERY_LENGTH]
        body: dict[str, Any] = {
            "query": query,
            "limit": max(1, min(spec.limit, FIRECRAWL_MAX_LIMIT)),
            # Object form, per current docs. A list of bare strings is the older
            # shape shown in the StockBrain spec and is not what is sent.
            "sources": [{"type": "web"}, {"type": "news"}],
            "country": spec.country,
        }
        if spec.freshness:
            body["tbs"] = spec.freshness
        if spec.include_domains:
            body["includeDomains"] = spec.include_domains
        if spec.exclude_domains:
            body["excludeDomains"] = spec.exclude_domains
        if spec.scrape_content:
            body["scrapeOptions"] = {
                "formats": [{"type": "markdown"}],
                "onlyMainContent": True,
            }
        return body

    async def search(self, spec: DiscoveryQuerySpec) -> Sequence[RawSourceDocument]:
        payload = await self._http.request_json(
            "POST",
            FIRECRAWL_SEARCH_PATH,
            json_body=self.build_request(spec),
            # A search has no side effect beyond credits, so a request that
            # failed transiently may be repeated. This is an explicit,
            # per-call decision -- never a global retry policy.
            retry_safe=True,
            max_attempts=3,
        )
        return self.parse_response(payload, spec)

    def parse_response(self, payload: Any, spec: DiscoveryQuerySpec) -> list[RawSourceDocument]:
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
        self.last_credits_used = int(credits_used) if isinstance(credits_used, int | float) else 0
        if self.last_credits_used:
            METRICS.inc("stockbrain_firecrawl_credits_total", float(self.last_credits_used))

        documents: list[RawSourceDocument] = []
        for source_type in ("web", "news"):
            entries = data.get(source_type)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                document = self._to_document(entry, source_type, spec)
                if document is not None:
                    documents.append(document)

        if warning := payload.get("warning"):
            log.warning("firecrawl_search_warning", warning=str(warning)[:300], query=spec.query)

        METRICS.inc(
            "stockbrain_news_items_received_total",
            len(documents),
            labels={"provider": "firecrawl", "mode": "search"},
        )
        return documents

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
        # markdown, when present, is the better body for both.
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
