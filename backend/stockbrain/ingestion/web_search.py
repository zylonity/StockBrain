"""Provider-agnostic web discovery: the canonical DTOs and the interface.

Phase 2 through Phase 9 discovered the broad web through exactly one vendor, and
the vendor's response shape leaked: ``sources`` was a Firecrawl concept, ``tbs``
was a Firecrawl token, and the job type was named after the company.  Replacing
the vendor therefore meant touching the scheduler, the job registry, the
handlers, the config and the GUI.  That is the cost this module exists to
remove.

Everything downstream of a search -- deduplication, the deterministic filters,
the DeepSeek triage, the event flow -- sees :class:`WebSearchResult` and nothing
else.  A provider adapter's only job is to turn its own JSON into these fields
and to keep the original payload alongside for audit.

Two rules the adapters must honour, both learned expensively:

**Searching never fetches pages.**  A search returns metadata: title, URL,
snippet, date, domain.  That is enough to deduplicate, to apply the source
category rules and for the cheap classifier to decide whether anything here is
worth reading.  Firecrawl's ``scrapeOptions`` defaulting to on is what turned a
4-credit search into a 24-credit search, and the same trap exists on Exa in the
shape of ``contents``.  Neither is ever set from a schedule.

**A search is never retried.**  Retrying is not a second chance at a failed
call; on a metered endpoint it is a second call.  The durable per-query cooldown
is the retry.

All text carried here is **untrusted**.  It is data, never instruction.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from stockbrain.db.base import utcnow
from stockbrain.enums import SourceProvider, WebDiscoveryKind
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.ingestion.normalizer import classify_source_category

__all__ = [
    "MAX_SEARCH_QUERY_LENGTH",
    "WebDiscoveryProvider",
    "WebSearchOutcome",
    "WebSearchQuery",
    "WebSearchResult",
    "result_domain",
    "to_raw_document",
]

#: The tightest documented query ceiling across the supported providers (Brave:
#: 400 characters / 50 words).  Applied by the shared query model rather than by
#: each adapter, so a query that is too long for one backend cannot be stored,
#: pass validation, and then fail at the point of spending money.
MAX_SEARCH_QUERY_LENGTH = 400


class WebSearchQuery(BaseModel):
    """One search to execute, expressed in StockBrain's own vocabulary.

    Deliberately free of provider tokens.  ``freshness_days`` is a number of
    days rather than ``qdr:d`` or ``pw`` or an ISO instant, because those are
    three spellings of one idea and only the adapter should know which.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=MAX_SEARCH_QUERY_LENGTH)
    kind: WebDiscoveryKind = WebDiscoveryKind.ROUTINE
    limit: int = Field(default=10, ge=1, le=100)
    """How many results to ask for.  A cost knob on every provider here."""

    freshness_days: int | None = Field(default=None, ge=1, le=3650)
    """Only results published within this many days, where the provider can
    express it.  ``None`` means no date constraint."""

    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    country: str = "US"
    category: str | None = None
    """Provider-side topical narrowing, currently only meaningful to Exa
    (``news``, ``company``, ``financial report``, ...).  Ignored by adapters
    that have no equivalent, rather than approximated into one."""


class WebSearchResult(BaseModel):
    """One canonical search hit.

    Every field except ``url``, ``provider`` and ``retrieved_at`` is optional,
    because "the provider did not say" is a real and common answer and is not
    the same as a default.  Inventing a ``published_at`` from the retrieval time
    would put a fabricated timestamp in front of the classifier.
    """

    model_config = ConfigDict(extra="forbid")

    provider: SourceProvider
    url: str
    title: str | None = None
    snippet: str | None = None
    published_at: dt.datetime | None = None
    source_domain: str | None = None
    provider_result_id: str | None = None
    """The provider's own handle for this result, where it issues one.  Exa's
    ``id`` is the input to its ``/contents`` endpoint; Brave issues none."""

    score: float | None = None
    """Relevance as the provider scored it, when the number means something.
    A ranking feature for display and ordering -- never a trading signal, and
    never comparable across providers."""

    result_kind: str | None = None
    """Which slice of the provider's response this came from: ``web``,
    ``news``, ``semantic``.  Kept because a news hit and a web hit deserve
    different weight and the distinction is free at parse time."""

    retrieved_at: dt.datetime = Field(default_factory=utcnow)
    raw: dict[str, Any] = Field(default_factory=dict)
    """The provider's original object, for audit and for re-reading a decision
    against exactly what arrived.  Never indexed into by downstream code."""


@dataclass(frozen=True, slots=True)
class WebSearchOutcome:
    """A search's canonical results plus what the call itself cost.

    Cost is part of the return value rather than an attribute on the client for
    the reason Phase 9 recorded: a counter on a long-lived object does not
    survive a restart and cannot refuse anything.  The caller writes it to the
    ledger in the same flow as the work it paid for.

    ``results_returned`` counts what the provider billed for, which is not
    ``len(results)``: a hit with no URL has no identity and is dropped, but it
    was returned and charged for all the same.
    """

    results: tuple[WebSearchResult, ...]
    results_returned: int
    billed_units_reported: int | None = None
    """The provider's own count of billable units, where it reports one."""

    cost_usd_reported: Decimal | None = None
    """The provider's own price for this call, where it reports one.  Exa
    returns ``costDollars``; Brave and Firecrawl report nothing per call."""

    warning: str | None = None


@runtime_checkable
class WebDiscoveryProvider(Protocol):
    """A search backend.

    Transport only.  A provider knows how to build a documented request and
    parse a documented response; it does not know whether it is allowed to make
    the call.  That belongs to the durable budget, which needs a database -- and
    a provider adapter that needs a database is one that cannot be tested
    against a recorded payload.
    """

    name: str

    provider: SourceProvider
    """Which ``SourceProvider`` this backend stamps on the rows it discovers."""

    supported_kinds: frozenset[WebDiscoveryKind]
    """Which query kinds this backend is a sensible answer to.  Enforced by the
    caller so that a misconfiguration is a refusal rather than an expensive
    surprise."""

    async def search(self, query: WebSearchQuery) -> WebSearchOutcome: ...

    async def aclose(self) -> None: ...


def result_domain(url: str) -> str | None:
    """The registrable-ish host of a result URL, or ``None`` if there isn't one."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def to_raw_document(
    result: WebSearchResult,
    *,
    discovery_query: str,
    kind: WebDiscoveryKind,
) -> RawSourceDocument:
    """Convert one canonical hit into the document the ingestion layer stores.

    The body is the snippet and only the snippet.  A search result is not an
    article: what is stored here is enough to deduplicate against, enough for
    the deterministic source-category rules, and enough for the cheap classifier
    to triage -- and fetching the article before any of that has run is spending
    on something nothing has yet judged worth reading.
    """
    return RawSourceDocument(
        provider=result.provider,
        provider_item_id=result.provider_result_id,
        url=result.url,
        source_name=result.source_domain,
        source_category=classify_source_category(result.url, result.source_domain),
        headline=result.title,
        author=None,
        published_at=result.published_at,
        updated_at_source=None,
        body=result.snippet,
        symbols=[],
        raw_payload=result.raw,
        metadata={
            "discovery_provider": result.provider.value,
            "discovery_query": discovery_query,
            "discovery_kind": kind.value,
            "result_kind": result.result_kind,
            "provider_score": result.score,
            "retrieved_at": result.retrieved_at.isoformat(),
            # Named "summary" to match what the Firecrawl path wrote, so an
            # event assembled from a mix of old and new rows reads the same key.
            "summary": result.snippet,
        },
    )
