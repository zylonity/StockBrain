"""Ingestion provider interfaces and the canonical raw-document DTO.

Every discovery provider -- a news WebSocket, a web search, a filing feed --
converts its own payload into a :class:`RawSourceDocument` before anything else
in the system sees it.  Raw provider JSON is preserved on the document for
audit, but it never becomes the interchange format: nothing downstream indexes
into a provider-shaped dict.

All text carried here is **untrusted**.  It is data, never instruction.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from stockbrain.enums import SourceCategory, SourceProvider

__all__ = [
    "DiscoveryQuerySpec",
    "DiscoverySearchOutcome",
    "DiscoverySearchProvider",
    "FilingProvider",
    "NewsProvider",
    "RawSourceDocument",
]


class RawSourceDocument(BaseModel):
    """One retrieved artefact, normalised into StockBrain's own vocabulary.

    ``raw_payload`` keeps the provider's original object so a classification can
    be re-examined later against exactly what arrived.
    """

    model_config = ConfigDict(extra="forbid")

    provider: SourceProvider
    provider_item_id: str | None = None
    """Stable provider identifier: Alpaca article id, SEC accession number."""

    url: str | None = None
    source_name: str | None = None
    source_category: SourceCategory = SourceCategory.UNKNOWN
    headline: str | None = None
    author: str | None = None
    published_at: dt.datetime | None = None
    updated_at_source: dt.datetime | None = None

    body: str | None = None
    """Article/filing body as delivered.  May contain HTML; never rendered raw."""

    symbols: list[str] = Field(default_factory=list)
    """Provider-supplied ticker hints.  A hint, never an instrument decision."""

    is_distinct_event: bool = False
    """True when this artefact *is* the event, rather than a report about one.

    A news article is one outlet's account of something that happened, so five
    articles with the same headline are five views of one event. A regulatory
    filing is different: it is the primary record of its own occurrence, and two
    filings are never the same event however similar their titles look.

    This matters because filing titles are templated. Five Form 4 filings by
    five different insiders all render as "Apple Inc. filed Form 4", and grouping
    them by headline would silently merge five distinct events into one --
    exactly the failure mode that is hardest to notice after the fact.
    """

    raw_payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DiscoveryQuerySpec(BaseModel):
    """A single thematic search to execute."""

    model_config = ConfigDict(extra="forbid")

    query: str
    limit: int = 10
    freshness: str | None = None
    """Firecrawl ``tbs`` token, e.g. ``qdr:h`` / ``qdr:d`` / ``qdr:w``."""

    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    country: str = "US"
    scrape_content: bool = False
    """Whether to attach ``scrapeOptions`` to the search.

    **Default false, and the scheduler never sets it.**  Firecrawl charges one
    credit per result page scraped on top of the search itself, so a
    twenty-result thematic search with this on costs twenty-four credits
    instead of four.  It defaulted to ``True`` through Phase 8, which is the
    single largest contributor to the credit incident recorded in
    ``docs/sources.md``.  Full content is fetched separately, per surviving
    result, by ``FIRECRAWL_ENRICH``."""


@runtime_checkable
class NewsProvider(Protocol):
    """Continuous news delivery plus bounded historical recovery."""

    name: str

    def stream(self) -> AsyncIterator[RawSourceDocument]:
        """Yield documents until cancelled.  Implementations reconnect internally."""
        ...

    async def backfill(
        self, start: dt.datetime, end: dt.datetime, *, limit: int | None = None
    ) -> list[RawSourceDocument]:
        """Fetch items in a window, used to close a gap after a disconnect."""
        ...


@dataclass(frozen=True, slots=True)
class DiscoverySearchOutcome:
    """A search's documents plus what the call itself cost.

    The cost is part of the return value rather than an attribute on the client
    because a metered provider's spend has to be recorded durably by the caller,
    in the same transaction as the work it paid for.  A counter living on a
    long-lived client object is a counter that disappears on restart -- which is
    precisely how the Firecrawl credit allowance was emptied unnoticed.

    ``results_returned`` counts what the provider billed for.  It is not
    ``len(documents)``: a result with no URL has no identity and is dropped, but
    it was returned and charged all the same.
    """

    documents: tuple[RawSourceDocument, ...]
    results_returned: int
    credits_reported: int | None = None
    """The provider's own accounting, when it supplies one."""

    warning: str | None = None
    scraped_pages: int = 0
    """How many results carried body content.  Non-zero only when the caller
    explicitly asked for content, which costs extra per page."""


@runtime_checkable
class DiscoverySearchProvider(Protocol):
    """Broad-web thematic discovery."""

    name: str

    async def search(self, spec: DiscoveryQuerySpec) -> DiscoverySearchOutcome: ...


@runtime_checkable
class FilingProvider(Protocol):
    """Authoritative regulatory filing discovery."""

    name: str

    async def recent_for_cik(
        self, cik: str, *, forms: Sequence[str] | None = None, limit: int = 20
    ) -> Sequence[RawSourceDocument]: ...

    async def recent_global(
        self, *, forms: Sequence[str] | None = None, limit: int = 100
    ) -> Sequence[RawSourceDocument]: ...
