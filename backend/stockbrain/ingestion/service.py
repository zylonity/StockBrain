"""Source ingestion: persist, deduplicate, and group into canonical events.

This is the single entry point every discovery provider funnels through, so the
deduplication rules cannot differ between the news stream, the search scheduler
and the filing poller.

Concurrency is handled by the database, not by locks in Python. Two workers
ingesting the same article simultaneously both pass the SELECT-based duplicate
check; the loser then violates ``uq_sources_provider_item`` (or
``uq_sources_canonical_url_hash``) and is reported as a duplicate. That is why
those indexes exist: a check-then-insert is a race, a unique index is not.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.base import utcnow
from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.db.models.system import AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import (
    ActorType,
    EventSourceRelationship,
    EventStatus,
    ExtractionMethod,
    JobType,
    SourceProvider,
)
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.ingestion.dedupe import (
    DEFAULT_EVENT_MATCH_WINDOW,
    find_duplicate_source,
    find_matching_event,
    title_hash,
)
from stockbrain.ingestion.normalizer import (
    NormalizedSource,
    html_to_text,
    normalize_document,
)
from stockbrain.jobs.queue import JobQueue
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = [
    "METADATA_ONLY_PROVIDERS",
    "IngestionOutcome",
    "IngestionResult",
    "IngestionService",
]

log = get_logger(__name__)

#: Providers whose search results carry metadata only, so a body has to be
#: fetched separately if it is wanted at all.  Alpaca and SEC are absent because
#: they deliver the article or the filing with the item, and re-fetching their
#: URL would store a second copy of something already held.
#:
#: ``FIRECRAWL`` is here because rows discovered by the Phase 2-9 Firecrawl
#: search are still in the table with only a snippet.  They remain eligible for
#: (free, local) extraction; what changed is that nothing discovers new ones.
METADATA_ONLY_PROVIDERS: frozenset[SourceProvider] = frozenset(
    {SourceProvider.BRAVE, SourceProvider.EXA, SourceProvider.FIRECRAWL}
)

#: Headline used when a provider supplies none. Never silently invented content:
#: the placeholder is obvious and the body still carries the evidence.
_UNTITLED = "(untitled source)"


#: Called with the open session and the new event's id, once per event that is
#: actually created.  Deliberately not called for a duplicate source or for a
#: source linked onto an existing event: those are not new stories.
EventCreatedHook = Callable[[AsyncSession, uuid.UUID], Awaitable[None]]


class IngestionOutcome(StrEnum):
    CREATED_EVENT = "CREATED_EVENT"
    """New source, new canonical event."""

    LINKED_TO_EVENT = "LINKED_TO_EVENT"
    """New source describing an event we already track (syndication)."""

    DUPLICATE_SOURCE = "DUPLICATE_SOURCE"
    """The same artefact has already been ingested. Nothing was written."""


@dataclass(slots=True)
class IngestionResult:
    outcome: IngestionOutcome
    source_id: uuid.UUID | None
    event_id: uuid.UUID | None
    detail: str | None = None


class IngestionService:
    """Persists raw documents as sources and groups them into events."""

    def __init__(
        self,
        database: Database,
        *,
        queue: JobQueue | None = None,
        event_match_window: dt.timedelta = DEFAULT_EVENT_MATCH_WINDOW,
        classification_enabled: bool = False,
        on_event_created: EventCreatedHook | None = None,
    ) -> None:
        self._database = database
        self._queue = queue or JobQueue()
        self._event_match_window = event_match_window
        # A callback rather than a notifier, so ingestion knows nothing about
        # Telegram, preferences or notification categories. It reports that an
        # event exists; whoever wired the hook decides whether that is worth
        # telling anyone. Called inside the ingestion transaction, so an
        # announcement cannot outlive the event it announces.
        self._on_event_created = on_event_created
        # Only enqueue classification when a handler actually exists to run it.
        # Creating jobs that nothing can process would fill the queue with rows
        # destined to fail their retry budget.
        self._classification_enabled = classification_enabled

    async def ingest(self, document: RawSourceDocument) -> IngestionResult:
        """Ingest one document. Idempotent with respect to the same artefact."""
        normalized = normalize_document(document)
        try:
            async with self._database.transaction() as session:
                return await self.ingest_in_session(session, normalized)
        except IntegrityError as exc:
            # Another worker won the race between our check and our insert.
            # The unique index is the authority; report a duplicate.
            METRICS.inc("stockbrain_events_deduped_total", labels={"layer": "unique_index_race"})
            log.debug(
                "ingest_duplicate_race",
                provider=document.provider.value,
                provider_item_id=document.provider_item_id,
                constraint=_constraint_name(exc),
            )
            return IngestionResult(
                outcome=IngestionOutcome.DUPLICATE_SOURCE,
                source_id=None,
                event_id=None,
                detail="lost insert race to a concurrent worker",
            )

    async def ingest_in_session(
        self, session: AsyncSession, normalized: NormalizedSource
    ) -> IngestionResult:
        """Ingest within a caller-supplied transaction."""
        document = normalized.document

        duplicate = await find_duplicate_source(session, normalized)
        if duplicate is not None:
            METRICS.inc("stockbrain_events_deduped_total", labels={"layer": duplicate.reason.value})
            existing_event_id = await self._event_id_for_source(session, duplicate.source.id)
            # Two providers finding one page is corroboration worth keeping, and
            # emphatically not two events. The second provider is appended to
            # the existing row's provenance and nothing else changes: no new
            # source, no new event, no second classification.
            corroborated = _record_provenance(duplicate.source, document.provider)
            log.debug(
                "ingest_duplicate",
                provider=document.provider.value,
                reason=duplicate.reason.value,
                source_id=str(duplicate.source.id),
                corroborated=corroborated,
            )
            if corroborated:
                METRICS.inc(
                    "stockbrain_discovery_corroborations_total",
                    labels={"provider": document.provider.value},
                )
            return IngestionResult(
                outcome=IngestionOutcome.DUPLICATE_SOURCE,
                source_id=duplicate.source.id,
                event_id=existing_event_id,
                detail=duplicate.reason.value,
            )

        source = Source(
            provider=document.provider,
            provider_item_id=document.provider_item_id,
            canonical_url=normalized.canonical_url,
            original_url=document.url,
            source_name=document.source_name,
            source_category=normalized.source_category.value,
            headline=document.headline,
            author=document.author,
            published_at=document.published_at,
            updated_at_source=document.updated_at_source,
            received_at=utcnow(),
            raw_content=document.body,
            normalized_text=normalized.normalized_text,
            content_hash=normalized.content_hash,
            # Who found it first. A second provider that later surfaces the same
            # page is appended rather than overwriting: `provider` owns the
            # row's identity and its uniqueness constraint.
            discovered_by=[document.provider.value],
            # Alpaca and SEC deliver the body with the item; a search provider
            # delivers a snippet and the body is fetched later, if at all.
            extraction_method=(
                ExtractionMethod.PROVIDER.value
                if document.provider not in METADATA_ONLY_PROVIDERS
                else None
            ),
            provider_metadata={
                **document.metadata,
                "symbols": document.symbols,
                "raw_payload": document.raw_payload,
            },
        )
        session.add(source)
        await session.flush()

        existing_event = await find_matching_event(
            session, normalized, window=self._event_match_window
        )
        if existing_event is not None:
            session.add(
                EventSourceLink(
                    event_id=existing_event.id,
                    source_id=source.id,
                    relationship_type=EventSourceRelationship.CORROBORATING,
                )
            )
            existing_event.updated_at = utcnow()
            session.add(
                AuditLog(
                    actor_type=ActorType.SYSTEM,
                    actor_id="ingestion",
                    action="SOURCE_LINKED_TO_EVENT",
                    entity_type="event",
                    entity_id=existing_event.id,
                    details={
                        "source_id": str(source.id),
                        "provider": document.provider.value,
                        "match": "normalized_headline",
                    },
                )
            )
            METRICS.inc("stockbrain_events_deduped_total", labels={"layer": "NORMALIZED_HEADLINE"})
            log.info(
                "source_linked_to_event",
                event_id=str(existing_event.id),
                source_id=str(source.id),
                provider=document.provider.value,
            )
            return IngestionResult(
                outcome=IngestionOutcome.LINKED_TO_EVENT,
                source_id=source.id,
                event_id=existing_event.id,
                detail="matched an existing event by normalised headline",
            )

        event = Event(
            title=document.headline or _UNTITLED,
            title_hash=title_hash(document.headline),
            summary=None,
            first_seen_at=utcnow(),
            event_time=document.published_at,
            status=EventStatus.NEW,
            # Topics are the classifier's output; the discovery query that found
            # the document is already recorded on the source's metadata.
            topics=[],
        )
        session.add(event)
        await session.flush()

        session.add(
            EventSourceLink(
                event_id=event.id,
                source_id=source.id,
                relationship_type=EventSourceRelationship.PRIMARY,
            )
        )
        session.add(
            AuditLog(
                actor_type=ActorType.SYSTEM,
                actor_id="ingestion",
                action="EVENT_CREATED",
                entity_type="event",
                entity_id=event.id,
                details={
                    "source_id": str(source.id),
                    "provider": document.provider.value,
                    "source_category": normalized.source_category.value,
                    "canonical_url": normalized.canonical_url,
                },
            )
        )

        # Classification is the next stage and runs out of band: ingestion must
        # never block on an LLM. The dedupe key makes re-enqueueing harmless.
        # Until the classifier ships, events simply wait in NEW status.
        if self._classification_enabled:
            await self._queue.enqueue(
                session,
                JobType.CLASSIFY_EVENT,
                payload={"event_id": str(event.id)},
                dedupe_key=f"classify:{event.id}",
                priority=20,
            )

        if self._on_event_created is not None:
            await self._on_event_created(session, event.id)

        METRICS.inc("stockbrain_events_created_total", labels={"provider": document.provider.value})
        log.info(
            "event_created",
            event_id=str(event.id),
            source_id=str(source.id),
            provider=document.provider.value,
            source_category=normalized.source_category.value,
        )
        return IngestionResult(
            outcome=IngestionOutcome.CREATED_EVENT, source_id=source.id, event_id=event.id
        )

    async def attach_fetched_content(
        self,
        source_id: uuid.UUID,
        *,
        body: str | None,
        fetched_url: str | None = None,
        status_code: int | None = None,
        method: ExtractionMethod = ExtractionMethod.PROVIDER,
        failure: str | None = None,
        detail: str | None = None,
    ) -> bool:
        """Store content fetched for an already-ingested source.

        The second half of the two-stage discovery model: stage one persisted a
        title, a URL and a snippet, and this writes the article that extraction
        returned for it.

        Three things it deliberately does **not** do:

        * It does not recompute ``content_hash``.  The hash is the row's
          identity for deduplication, and two searches that returned the same
          URL must keep colliding on ``uq_sources_canonical_url_hash`` after one
          of them has been enriched.  Rewriting the hash would make the same
          article ingestable a second time.
        * It does not re-run event grouping.  The source already belongs to an
          event; a longer body is not new evidence of a different one.
        * It does not overwrite existing content with nothing.  An extraction
          that came back empty marks the attempt as made -- so nothing tries the
          same URL again, and nothing pays to -- and leaves the snippet in
          place.

        Returns whether a body was actually stored.
        """
        async with self._database.transaction() as session:
            source = await session.get(Source, source_id)
            if source is None:
                return False
            stored = bool(body and body.strip())
            if stored:
                source.raw_content = body
                source.normalized_text = html_to_text(body)
            # Set either way: the fetch happened, and the point of the column is
            # to stop it happening again.
            source.content_fetched_at = utcnow()
            source.extraction_method = method.value if stored else ExtractionMethod.NONE.value
            source.provider_metadata = {
                **source.provider_metadata,
                "content_fetch": {
                    "fetched_url": fetched_url,
                    "status_code": status_code,
                    "stored": stored,
                    "method": method.value,
                    "failure": failure,
                    # A short, credential-free explanation. Never a provider
                    # body: an error body can echo the request, and the request
                    # carries an Authorization header.
                    "detail": detail[:300] if detail else None,
                },
            }
            session.add(
                AuditLog(
                    actor_type=ActorType.SYSTEM,
                    actor_id="ingestion",
                    action="SOURCE_CONTENT_FETCHED",
                    entity_type="source",
                    entity_id=source_id,
                    details={
                        "stored": stored,
                        "status_code": status_code,
                        "content_length": len(body or ""),
                        "method": method.value,
                        "failure": failure,
                    },
                )
            )
        log.info(
            "source_content_fetched",
            source_id=str(source_id),
            stored=stored,
            method=method.value,
        )
        return stored

    async def enqueue_content_fetches(self, *, limit: int) -> int:
        """Offer up to ``limit`` triaged sources for a content fetch.

        The triage is the classifier's, not this method's: only a source whose
        event the classifier promoted to ``CANDIDATE`` is offered, which is what
        makes the fetch a fetch of something already judged interesting.  That
        ordering is the whole cheap-first pipeline -- search metadata, then
        deterministic dedupe, then the cheap classifier, and only then a page.

        Restricted to providers that deliver *metadata only*.  An Alpaca article
        and an SEC filing arrive with their body attached, so fetching their URL
        would be a second copy of something already stored.

        ``limit`` is the caller's bound and the durable budgets are the real
        ones -- this only enqueues, and a job that finds the budget exhausted
        spends nothing.
        """
        enqueued = 0
        async with self._database.transaction() as session:
            rows = (
                await session.execute(
                    sa.select(Source.id)
                    .join(EventSourceLink, EventSourceLink.source_id == Source.id)
                    .join(Event, Event.id == EventSourceLink.event_id)
                    .where(
                        Source.provider.in_(METADATA_ONLY_PROVIDERS),
                        Source.content_fetched_at.is_(None),
                        Event.status == EventStatus.CANDIDATE,
                    )
                    .order_by(Source.received_at.desc())
                    .limit(max(1, limit))
                )
            ).scalars()
            for source_id in rows:
                job_id = await self._queue.enqueue(
                    session,
                    JobType.CONTENT_EXTRACT,
                    payload={"source_id": str(source_id)},
                    # One outstanding fetch per source. Two jobs for one URL is
                    # two requests for one article, and possibly two charges.
                    dedupe_key=f"extract:{source_id}",
                    priority=70,
                    # One attempt: the sweep offers it again next tick if it is
                    # still wanted, and that path re-checks the budget. A queue
                    # retry would be a second reservation for the same page.
                    max_attempts=1,
                )
                if job_id is not None:
                    enqueued += 1
        if enqueued:
            log.info("content_fetches_enqueued", count=enqueued)
        return enqueued

    async def ingest_many(self, documents: list[RawSourceDocument]) -> list[IngestionResult]:
        """Ingest a batch, one transaction each.

        Deliberately not one transaction for the whole batch: a single malformed
        document must not discard the rest of a search's results.
        """
        return [await self.ingest(document) for document in documents]

    @staticmethod
    async def _event_id_for_source(session: AsyncSession, source_id: uuid.UUID) -> uuid.UUID | None:
        stmt = (
            sa.select(EventSourceLink.event_id)
            .where(EventSourceLink.source_id == source_id)
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()


def _record_provenance(source: Source, provider: SourceProvider) -> bool:
    """Append a provider to a source's ``discovered_by``.  Returns whether it was new.

    Ordered and de-duplicated, so the list reads as "Brave found it, then Exa
    confirmed it" rather than as a set with no history.  The row's ``provider``
    column is never touched: it records who found it first, it is what the
    unique index is built on, and rewriting it would falsify provenance.
    """
    existing = list(source.discovered_by or [])
    if not existing:
        existing = [source.provider.value]
    if provider.value in existing:
        source.discovered_by = existing
        return False
    source.discovered_by = [*existing, provider.value]
    return True


def _constraint_name(exc: IntegrityError) -> str | None:
    original = getattr(exc, "orig", None)
    constraint = getattr(original, "constraint_name", None)
    return str(constraint) if constraint else None
