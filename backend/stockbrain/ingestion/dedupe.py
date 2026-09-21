"""Layered deduplication.

The layers run cheapest-first, and every layer in this module is deterministic:

* **Layer 1 -- provider identity.** The same Alpaca article id or SEC accession
  number is the same artefact, full stop.
* **Layer 2 -- canonical URL.** The same document reached through different
  tracking parameters or hosts.
* **Layer 3 -- content hash.** Verbatim syndication of one wire story across
  outlets, where the URLs legitimately differ.
* **Layer 3.5 -- exact normalised headline within a time window.** Groups
  distinct *sources* onto one *event*, so a story carried by five outlets
  produces one research run rather than five.

**Layer 4 (semantic "is this the same event?") is deliberately absent here.** It
requires an LLM call and belongs to the classifier phase; :func:`event_match_gap`
documents the hook. Running an expensive model before these free checks would be
the wrong order.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.base import utcnow
from stockbrain.db.models.sources import Event, Source
from stockbrain.enums import EventStatus
from stockbrain.ingestion.normalizer import NormalizedSource, content_hash, normalize_whitespace

__all__ = [
    "DEFAULT_EVENT_MATCH_WINDOW",
    "DuplicateReason",
    "SourceMatch",
    "event_match_gap",
    "find_duplicate_source",
    "find_matching_event",
    "title_hash",
]

#: How far back an incoming headline is compared for event grouping. Long enough
#: to catch syndication lag, short enough that a recurring headline ("Fed holds
#: rates") in a later month is treated as a new event.
DEFAULT_EVENT_MATCH_WINDOW = dt.timedelta(hours=36)


class DuplicateReason(StrEnum):
    PROVIDER_ITEM_ID = "PROVIDER_ITEM_ID"
    CANONICAL_URL = "CANONICAL_URL"
    CONTENT_HASH = "CONTENT_HASH"


@dataclass(slots=True)
class SourceMatch:
    source: Source
    reason: DuplicateReason


def title_hash(headline: str | None) -> str:
    """Stable hash of a normalised headline, used to group sources onto events."""
    return content_hash(headline, None)


async def find_duplicate_source(
    session: AsyncSession, normalized: NormalizedSource
) -> SourceMatch | None:
    """Return an existing source that is the same artefact, if any.

    Layers are tried in increasing cost. Each returns the *reason* as well as the
    row, so the ingestion log records which layer caught the duplicate -- useful
    when tuning the URL canonicalisation rules.
    """
    document = normalized.document

    if document.provider_item_id:
        stmt = sa.select(Source).where(
            Source.provider == document.provider,
            Source.provider_item_id == document.provider_item_id,
        )
        existing = (await session.execute(stmt.limit(1))).scalar_one_or_none()
        if existing is not None:
            return SourceMatch(existing, DuplicateReason.PROVIDER_ITEM_ID)

    if normalized.canonical_url:
        stmt = sa.select(Source).where(Source.canonical_url == normalized.canonical_url)
        existing = (await session.execute(stmt.limit(1))).scalar_one_or_none()
        if existing is not None:
            return SourceMatch(existing, DuplicateReason.CANONICAL_URL)

    stmt = sa.select(Source).where(Source.content_hash == normalized.content_hash)
    existing = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if existing is not None and not (
        document.is_distinct_event and document.provider_item_id
    ):
        return SourceMatch(existing, DuplicateReason.CONTENT_HASH)

    return None


async def find_matching_event(
    session: AsyncSession,
    normalized: NormalizedSource,
    *,
    window: dt.timedelta = DEFAULT_EVENT_MATCH_WINDOW,
    now: dt.datetime | None = None,
) -> Event | None:
    """Find a recent event with an identical normalised headline.

    Exact match only. Fuzzy or semantic matching is the classifier's job: getting
    it wrong here would silently merge two genuinely different events, and a
    merged event is much harder to notice than a duplicated one.

    Archived events are excluded, so a headline recurring after an event has been
    closed out starts a fresh event rather than reviving a stale one.

    Documents flagged ``is_distinct_event`` (regulatory filings) skip this layer
    entirely: their titles are templated, so headline matching would merge
    unrelated filings.
    """
    if normalized.document.is_distinct_event:
        return None
    if not normalized.normalized_headline:
        return None

    cutoff = (now or utcnow()) - window
    stmt = (
        sa.select(Event)
        .where(
            Event.title_hash == title_hash(normalized.document.headline),
            Event.first_seen_at >= cutoff,
            Event.status != EventStatus.ARCHIVED,
        )
        .order_by(Event.first_seen_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def event_match_gap(normalized: NormalizedSource) -> str | None:
    """Describe what a semantic matcher would still need to decide.

    Layers 1-3.5 catch identical artefacts and identical headlines. They do not
    catch a rewritten headline about the same underlying event, an update to a
    developing story, or a contradicting report. Those require the Layer 4
    classifier (``SAME_EVENT`` / ``UPDATE_TO_EVENT`` /
    ``RELATED_DIFFERENT_EVENT`` / ``UNRELATED``), which lands with the DeepSeek
    classifier. Returns a short reason string when this document is a candidate
    for that check, or ``None`` when the deterministic layers were conclusive.
    """
    if not normalize_whitespace(normalized.document.headline):
        return "no headline to compare deterministically"
    return "rewritten headlines and story updates need semantic comparison"
