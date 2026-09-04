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
    JobType,
)
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.ingestion.dedupe import (
    DEFAULT_EVENT_MATCH_WINDOW,
    find_duplicate_source,
    find_matching_event,
    title_hash,
)
from stockbrain.ingestion.normalizer import NormalizedSource, normalize_document
from stockbrain.jobs.queue import JobQueue
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["IngestionOutcome", "IngestionResult", "IngestionService"]

log = get_logger(__name__)

#: Headline used when a provider supplies none. Never silently invented content:
#: the placeholder is obvious and the body still carries the evidence.
_UNTITLED = "(untitled source)"


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
    ) -> None:
        self._database = database
        self._queue = queue or JobQueue()
        self._event_match_window = event_match_window
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
            log.debug(
                "ingest_duplicate",
                provider=document.provider.value,
                reason=duplicate.reason.value,
                source_id=str(duplicate.source.id),
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


def _constraint_name(exc: IntegrityError) -> str | None:
    original = getattr(exc, "orig", None)
    constraint = getattr(original, "constraint_name", None)
    return str(constraint) if constraint else None
