"""Brave Search: the default routine thematic web/news discovery provider.

Verified against Brave's current documentation on **2026-09-05**
(<https://api-dashboard.search.brave.com/api-reference/web/search/get>,
``/documentation/guides/rate-limiting``, <https://brave.com/search/api/>):

* ``GET https://api.search.brave.com/res/v1/web/search``
* auth is the header ``X-Subscription-Token`` -- not a bearer token
* ``q`` is capped at **400 characters and 50 words**
* ``count`` is 1-20 and applies to **web results only**; ``offset`` is 0-9
* ``freshness`` is ``pd`` / ``pw`` / ``pm`` / ``py`` or ``YYYY-MM-DDtoYYYY-MM-DD``
* ``result_filter`` is a comma-separated subset of ``discussions``, ``faq``,
  ``infobox``, ``news``, ``query``, ``summarizer``, ``videos``, ``web``,
  ``locations``.  **One request returns both the web and the news cluster**, so
  the separate ``/res/v1/news/search`` endpoint -- which is a second billable
  request -- is deliberately not used.
* the response is ``{"type", "query", "web": {"results": [...]},
  "news": {"results": [...]}, ...}``; a web result carries ``title``, ``url``,
  ``description``, ``page_age``, ``age`` and ``meta_url.hostname``, and a news
  result carries the same minus ``profile`` plus ``breaking``
* rate limiting answers **429** and every response carries ``X-RateLimit-Limit``,
  ``X-RateLimit-Policy`` (``1;w=1, 15000;w=2592000``), ``X-RateLimit-Remaining``
  and ``X-RateLimit-Reset``
* **only successful requests are counted against quota and billed.**  This is
  the opposite of Firecrawl, and it is why a Brave GET is allowed the shared
  client's bounded retry while a Firecrawl call is not.

Pricing, same date: **$5 per 1,000 requests**, with $5 of monthly credit applied
automatically -- roughly 1,000 requests a month at no cost.  The default caps in
``Settings`` are set an order of magnitude below that; see ``docs/sources.md``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from dateutil import parser as date_parser
from pydantic import BaseModel, ConfigDict, ValidationError

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider, WebDiscoveryKind
from stockbrain.errors import ProviderResponseError
from stockbrain.httpclient import ProviderHttpClient, TokenBucket
from stockbrain.ingestion.web_search import (
    WebSearchOutcome,
    WebSearchQuery,
    WebSearchResult,
    result_domain,
)
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = [
    "BRAVE_MAX_COUNT",
    "BRAVE_MAX_QUERY_LENGTH",
    "BRAVE_MAX_QUERY_WORDS",
    "BRAVE_SEARCH_PATH",
    "BraveSearchClient",
    "brave_freshness_token",
    "trim_brave_query",
]

log = get_logger(__name__)

BRAVE_SEARCH_PATH = "/res/v1/web/search"
BRAVE_MAX_QUERY_LENGTH = 400
BRAVE_MAX_QUERY_WORDS = 50
BRAVE_MAX_COUNT = 20


class BraveMetaUrl(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hostname: str | None = None
    netloc: str | None = None


class BraveResult(BaseModel):
    """One web or news hit.

    The two shapes are near-identical and the differences are all optional, so
    one model reads both rather than two models that would drift apart.
    """

    model_config = ConfigDict(extra="ignore")

    url: str | None = None
    title: str | None = None
    description: str | None = None
    age: str | None = None
    """A human phrase such as "2 days ago". Never parsed as a timestamp."""

    page_age: str | None = None
    """An ISO-8601 instant when present -- this is the one usable date."""

    breaking: bool | None = None
    meta_url: BraveMetaUrl | None = None
    extra_snippets: list[str] | None = None


class BraveSearchClient:
    """Routine thematic web and news search.

    Not the fast financial-news path: Alpaca news and SEC EDGAR do that, and
    both are unmetered by comparison.  This finds the thematic and second-hand
    developments a finance-only feed never carries.
    """

    name = "brave"
    provider = SourceProvider.BRAVE
    supported_kinds = frozenset({WebDiscoveryKind.ROUTINE})

    def __init__(self, settings: Settings, *, http: ProviderHttpClient | None = None) -> None:
        self._settings = settings
        self._http = http or ProviderHttpClient(
            provider="brave",
            base_url=settings.brave_base_url,
            headers={
                "X-Subscription-Token": settings.brave_api_key.get_secret_value(),
                "Accept": "application/json",
                # Brave's documentation asks for this explicitly; without it the
                # API may answer an uncompressed body it considers deprecated.
                "Accept-Encoding": "gzip",
            },
            timeout_seconds=settings.brave_timeout_seconds,
            # The documented burst ceiling on the entry plan is 1 request per
            # second. Staying under a limit is cheaper than discovering it.
            rate_limiter=TokenBucket(rate_per_second=1.0, burst=1),
            max_attempts=2,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def outbound_headers(self) -> dict[str, str]:
        """The headers this client will actually send, lower-cased.

        Exposed because "does it authenticate the way the docs say" is worth an
        assertion, and reading it back from the constructed client is the only
        way to assert it without re-stating the constructor in a test.
        """
        return {key.lower(): value for key, value in self._http.headers.items()}

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------
    def result_filters(self) -> list[str]:
        """The configured ``result_filter`` values.

        Unlike Firecrawl's ``sources``, this does **not** multiply the price: one
        request is one billable request however many clusters come back.  It is
        a relevance knob, not a cost knob.
        """
        return list(self._settings.brave_result_filter)

    def build_params(self, query: WebSearchQuery) -> dict[str, Any]:
        """Build the query string, in the shape the current API documents."""
        params: dict[str, Any] = {
            "q": trim_brave_query(query.query),
            # Per request, and web-only per the documentation. The news cluster
            # comes back alongside without a second charge.
            "count": max(1, min(query.limit, BRAVE_MAX_COUNT)),
            "country": query.country,
            "result_filter": ",".join(self.result_filters()),
            # Markup in a title is noise to a classifier and a hazard to a
            # renderer. Brave defaults this to true.
            "text_decorations": False,
            "safesearch": self._settings.brave_safesearch,
            "spellcheck": False,
        }
        freshness = brave_freshness_token(query.freshness_days)
        if freshness:
            params["freshness"] = freshness
        return params

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------
    async def search(self, query: WebSearchQuery) -> WebSearchOutcome:
        """Run one search.

        ``get_json`` retries bounded and only on 429/5xx.  That is safe here
        specifically because Brave documents that only successful requests are
        billed -- the same retry against Firecrawl would be a second charge.
        """
        payload = await self._http.get_json(
            BRAVE_SEARCH_PATH,
            params=self.build_params(query),
            max_attempts=2,
        )
        return self.parse_response(payload, query)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def parse_response(self, payload: Any, query: WebSearchQuery) -> WebSearchOutcome:
        if not isinstance(payload, dict):
            raise ProviderResponseError("brave: response was not a JSON object")

        results: list[WebSearchResult] = []
        returned = 0
        seen: set[str] = set()
        for cluster in ("web", "news"):
            block = payload.get(cluster)
            if block is None:
                continue
            if not isinstance(block, dict):
                raise ProviderResponseError(f"brave: {cluster!r} was not an object")
            entries = block.get("results")
            if entries is None:
                continue
            if not isinstance(entries, list):
                raise ProviderResponseError(f"brave: {cluster}.results was not a list")
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                # Counted before the URL check and before deduplication: a hit
                # with no URL, or one the news cluster repeats from the web
                # cluster, is still a hit the provider returned.
                returned += 1
                parsed = self._to_result(entry, cluster, query)
                if parsed is None or parsed.url in seen:
                    continue
                seen.add(parsed.url)
                results.append(parsed)

        if not results and returned == 0 and "web" not in payload and "news" not in payload:
            # An answer with neither cluster and no error is a schema the
            # adapter does not understand. Say so rather than reporting "no
            # thematic news today", which is what a silent empty list means.
            raise ProviderResponseError("brave: response carried no 'web' or 'news' block")

        METRICS.inc(
            "stockbrain_news_items_received_total",
            len(results),
            labels={"provider": "brave", "mode": "search"},
        )
        return WebSearchOutcome(
            results=tuple(results),
            results_returned=returned,
            # One request, one billable unit, whatever came back.
            billed_units_reported=1,
            cost_usd_reported=None,
        )

    def _to_result(
        self, entry: dict[str, Any], cluster: str, query: WebSearchQuery
    ) -> WebSearchResult | None:
        try:
            parsed = BraveResult.model_validate(entry)
        except ValidationError as exc:
            log.warning("brave_result_schema_mismatch", cluster=cluster, errors=exc.error_count())
            return None
        if not parsed.url:
            return None

        snippet = parsed.description
        if not snippet and parsed.extra_snippets:
            snippet = parsed.extra_snippets[0]

        return WebSearchResult(
            provider=SourceProvider.BRAVE,
            url=parsed.url,
            title=parsed.title,
            snippet=snippet,
            published_at=_parse_page_age(parsed.page_age),
            source_domain=(parsed.meta_url.hostname if parsed.meta_url else None)
            or result_domain(parsed.url),
            # Brave issues no per-result identifier; the canonical URL is the
            # identity, and inventing one would create a second identity for the
            # same page.
            provider_result_id=None,
            score=None,
            result_kind=cluster,
            raw=entry,
        )


def trim_brave_query(query: str) -> str:
    """Clamp a stored query to Brave's documented 400-character / 50-word limit.

    Trimmed rather than rejected: a query that is one word too long should
    return slightly less, not nothing.  Words are cut before characters so the
    result is still a phrase.
    """
    words = query.strip().split()
    if len(words) > BRAVE_MAX_QUERY_WORDS:
        words = words[:BRAVE_MAX_QUERY_WORDS]
    trimmed = " ".join(words)
    if len(trimmed) > BRAVE_MAX_QUERY_LENGTH:
        trimmed = trimmed[:BRAVE_MAX_QUERY_LENGTH].rsplit(" ", 1)[0]
    return trimmed


def brave_freshness_token(days: int | None) -> str | None:
    """Map a number of days onto Brave's documented ``freshness`` vocabulary.

    Rounded **up** to the next documented bucket.  Asking for eight days and
    getting ``pw`` would silently drop the eighth day; ``pm`` returns a superset
    and the classifier discards what it does not want, which is free.
    """
    if days is None:
        return None
    if days <= 1:
        return "pd"
    if days <= 7:
        return "pw"
    if days <= 31:
        return "pm"
    if days <= 365:
        return "py"
    return None


def _parse_page_age(value: str | None) -> dt.datetime | None:
    """Parse ``page_age``, which is ISO-8601 when Brave supplies it.

    ``age`` ("2 days ago") is deliberately not parsed: it is a rendering, and
    turning a rendering back into an instant invents precision the provider did
    not offer.
    """
    if not value:
        return None
    try:
        parsed = date_parser.isoparse(value)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
