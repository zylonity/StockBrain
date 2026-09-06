"""Exa: semantic second-order discovery.

Not a second Brave.  Brave answers "what was published about grid transformers
this week"; Exa answers "which public companies benefit from a transformer
shortage caused by datacentre expansion" -- a question a keyword index answers
badly and an embedding index answers well, at roughly ten times the price per
call.  Running every routine query through both would double the bill to
rediscover the same articles, which is why the query taxonomy exists and why
this provider only accepts ``SEMANTIC`` queries.

Verified against Exa's current documentation on **2026-09-05**
(<https://exa.ai/docs/reference/search>, ``/reference/pricing``,
``/reference/rate-limits``):

* ``POST https://api.exa.ai/search``
* auth is ``Authorization: Bearer <key>`` with ``Content-Type: application/json``
* body: ``query`` (required), ``type`` (``instant`` / ``fast`` / ``auto``
  (default) / ``deep-lite`` / ``deep`` / ``deep-reasoning``), ``numResults``
  1-100 default 10, ``category``, ``startPublishedDate`` /
  ``endPublishedDate`` as ISO-8601, ``includeDomains`` / ``excludeDomains``,
  and ``contents``
* **content fields must nest under ``contents``**; a top-level ``text`` is a 400
* the response is ``{"requestId", "results": [{"id", "title", "url",
  "publishedDate", "author", "image", "favicon", "text"?, "highlights"?,
  "summary"?}], "costDollars": {"total", "search": {...}, "contents": {...}},
  "searchTime"}``
* ``/search`` is rate limited at **10 QPS**

Pricing, same date: **$7 per 1,000 requests for up to 10 results**, **$1 per
1,000 results above 10**, contents **$1 per 1,000 pages per content type**, and
``deep``/``deep-reasoning`` at $12-15 per 1,000.  New accounts get $20 of credit
and the free tier adds $10 a month.

Two consequences are load-bearing:

**``contents`` is never sent from a schedule.**  It is this API's ``scrapeOptions``
-- the field that quietly turns a metadata call into a page-fetch call, per
result.  Extraction happens after triage, locally, through
:mod:`stockbrain.extraction`.  ``EXA_FETCH_CONTENTS`` exists so the decision is
visible and off by default rather than absent.

**``numResults`` stays at or below 10.**  Eleven results is the base price plus
an overage line, for a semantic query whose whole value is in its first few
hits.

``costDollars`` is the provider's own price for the call and is recorded on the
ledger row, so the budget compares against Exa's number rather than only
StockBrain's estimate.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
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
    "EXA_INCLUDED_RESULTS",
    "EXA_MAX_RESULTS",
    "EXA_SEARCH_PATH",
    "ExaSearchClient",
]

log = get_logger(__name__)

EXA_SEARCH_PATH = "/search"

#: Results included in the base per-request price.  Above this each result is
#: separately billed, so it is the default ceiling rather than the API's 100.
EXA_INCLUDED_RESULTS = 10
EXA_MAX_RESULTS = 100


class ExaResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    url: str | None = None
    title: str | None = None
    publishedDate: str | None = None  # noqa: N815 - provider field name
    author: str | None = None
    score: float | None = None
    text: str | None = None
    summary: str | None = None
    highlights: list[str] | None = None


class ExaSearchClient:
    """Semantic discovery of second-order and indirect exposure."""

    name = "exa"
    provider = SourceProvider.EXA
    supported_kinds = frozenset({WebDiscoveryKind.SEMANTIC})

    def __init__(self, settings: Settings, *, http: ProviderHttpClient | None = None) -> None:
        self._settings = settings
        self._http = http or ProviderHttpClient(
            provider="exa",
            base_url=settings.exa_base_url,
            headers={
                "Authorization": f"Bearer {settings.exa_api_key.get_secret_value()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout_seconds=settings.exa_timeout_seconds,
            # Documented at 10 QPS. StockBrain issues a handful a day; the
            # limiter is here so a bug cannot become a burst.
            rate_limiter=TokenBucket(rate_per_second=1.0, burst=2),
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
    def build_request(
        self, query: WebSearchQuery, *, now: dt.datetime | None = None
    ) -> dict[str, Any]:
        """Build the search body, in the shape the current API documents.

        ``contents`` appears only when ``EXA_FETCH_CONTENTS`` is explicitly on,
        and even then only as plain text: ``summary`` is an extra $1/1k pages
        of LLM output for a job DeepSeek already does under StockBrain's own
        budget.
        """
        body: dict[str, Any] = {
            "query": query.query.strip(),
            "type": self._settings.exa_search_type,
            "numResults": max(1, min(query.limit, EXA_MAX_RESULTS)),
        }
        if query.category:
            body["category"] = query.category
        if query.freshness_days is not None:
            start = (now or dt.datetime.now(tz=dt.UTC)) - dt.timedelta(days=query.freshness_days)
            body["startPublishedDate"] = start.astimezone(dt.UTC).isoformat()
        if query.include_domains:
            body["includeDomains"] = list(query.include_domains)
        if query.exclude_domains:
            body["excludeDomains"] = list(query.exclude_domains)
        if self._settings.exa_fetch_contents:
            body["contents"] = {"text": True}
        return body

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------
    async def search(self, query: WebSearchQuery) -> WebSearchOutcome:
        """Run one semantic search.  **Never retried.**

        Exa does not publish a "failed requests are not billed" guarantee the
        way Brave does, so the conservative reading applies: a retry is a second
        call at $0.007, and the durable per-query cooldown is the retry.
        """
        payload = await self._http.request_json(
            "POST",
            EXA_SEARCH_PATH,
            json_body=self.build_request(query),
        )
        return self.parse_response(payload, query)

    async def verify_credentials(self) -> None:
        """Prove the API key is accepted, without buying a search.

        Exa prices a *successful* search and reports it in ``costDollars``, so
        this sends a body it cannot accept -- an empty object, with no
        ``query``.  Measured 2026-09-06 against the live API: a valid key
        answers **400** ``INVALID_REQUEST_BODY``, an invalid one answers
        **401** ``INVALID_API_KEY``.  The shared classifier already maps 401 to
        ``ProviderAuthError``, so unlike Brave no refinement is needed -- the
        two statuses are genuinely different.

        This is what lets a restarted container report Exa's health for
        nothing.  It matters more here than for Brave: the default allowance is
        three searches a day, so probing by searching would let a handful of
        restarts spend the entire semantic budget on health checks and leave
        the real queries deferring.

        Never retried -- ``request_json`` without ``retry_safe`` makes exactly
        one attempt.
        """
        try:
            await self._http.request_json("POST", EXA_SEARCH_PATH, json_body={})
        except ProviderResponseError:
            # The key was accepted; the empty body was refused, as intended.
            return

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def parse_response(self, payload: Any, query: WebSearchQuery) -> WebSearchOutcome:
        if not isinstance(payload, dict):
            raise ProviderResponseError("exa: response was not a JSON object")
        entries = payload.get("results")
        if entries is None:
            raise ProviderResponseError("exa: response carried no 'results'")
        if not isinstance(entries, list):
            raise ProviderResponseError("exa: 'results' was not a list")

        results: list[WebSearchResult] = []
        returned = 0
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            returned += 1
            parsed = self._to_result(entry, query)
            if parsed is None or parsed.url in seen:
                continue
            seen.add(parsed.url)
            results.append(parsed)

        cost = _cost_total(payload.get("costDollars"))
        METRICS.inc(
            "stockbrain_news_items_received_total",
            len(results),
            labels={"provider": "exa", "mode": "search"},
        )
        return WebSearchOutcome(
            results=tuple(results),
            results_returned=returned,
            billed_units_reported=1,
            cost_usd_reported=cost,
        )

    def _to_result(self, entry: dict[str, Any], query: WebSearchQuery) -> WebSearchResult | None:
        try:
            parsed = ExaResult.model_validate(entry)
        except ValidationError as exc:
            log.warning("exa_result_schema_mismatch", errors=exc.error_count())
            return None
        if not parsed.url:
            return None

        # Preference order is deliberate: a highlight is the passage Exa judged
        # relevant to *this* query, which is a better triage input than the
        # first paragraph of the page. Full text appears only when contents was
        # explicitly requested, and is truncated -- the classifier is given a
        # snippet at this stage by design.
        snippet: str | None = None
        if parsed.highlights:
            snippet = " ".join(h for h in parsed.highlights if h).strip() or None
        if not snippet:
            snippet = parsed.summary
        if not snippet and parsed.text:
            snippet = parsed.text[: self._settings.exa_snippet_max_chars]

        return WebSearchResult(
            provider=SourceProvider.EXA,
            url=parsed.url,
            title=parsed.title,
            snippet=snippet,
            published_at=_parse_published(parsed.publishedDate),
            source_domain=result_domain(parsed.url),
            # Exa's `id` is the handle its /contents endpoint takes. Kept for
            # provenance; StockBrain does not call that endpoint.
            provider_result_id=parsed.id,
            score=parsed.score,
            result_kind="semantic",
            raw=entry,
        )


def _cost_total(value: Any) -> Decimal | None:
    """Read ``costDollars.total`` -- the provider's own price for the call.

    Exa documents this as an estimate rather than an invoice, so it is recorded
    alongside StockBrain's own estimate rather than replacing it; the ledger
    charges the larger of the two.
    """
    if not isinstance(value, dict):
        return None
    total = value.get("total")
    if not isinstance(total, int | float | str):
        return None
    try:
        return Decimal(str(total))
    except (InvalidOperation, ValueError):
        return None


def _parse_published(value: str | None) -> dt.datetime | None:
    """Parse ``publishedDate``.

    Exa documents this as "an estimate of the creation date, from parsing HTML
    content", so it is a claim about the page rather than a fact from a
    publisher -- carried through, but never treated as authoritative.
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
