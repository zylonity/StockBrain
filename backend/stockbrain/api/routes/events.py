"""Event and source browsing.

Read-only. Source text is returned as extracted plain text with an explicit
excerpt limit; the raw provider body is never sent to the browser, because
rendering untrusted HTML is how a scraped article becomes an XSS vector.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from stockbrain.api.dependencies import DbSession
from stockbrain.api.schemas import (
    EventDetailResponse,
    EventListResponse,
    EventSummaryResponse,
    SourceResponse,
)
from stockbrain.db.models.sources import Event, Source
from stockbrain.db.repositories.events import EventFilters, EventRepository
from stockbrain.enums import EventStatus, SourceProvider

router = APIRouter(prefix="/api/v1", tags=["events"])

#: Excerpt length sent to the list/detail views. The full text stays in the
#: database for research; the browser does not need it to triage.
EXCERPT_CHARS = 1200


def _to_summary(
    event: Event, source_count: int, providers: list[str], category: str | None
) -> EventSummaryResponse:
    return EventSummaryResponse(
        id=event.id,
        title=event.title,
        summary=event.summary,
        status=event.status.value,
        event_type=event.event_type,
        first_seen_at=event.first_seen_at,
        event_time=event.event_time,
        importance_score=event.importance_score,
        novelty_score=event.novelty_score,
        source_count=source_count,
        providers=providers,
        top_category=category,
    )


def _to_source(source: Source, relationship: str | None) -> SourceResponse:
    metadata = source.provider_metadata or {}
    raw_symbols = metadata.get("symbols")
    symbols = [str(s) for s in raw_symbols] if isinstance(raw_symbols, list) else []
    excerpt = (source.normalized_text or "")[:EXCERPT_CHARS] or None
    return SourceResponse(
        id=source.id,
        provider=source.provider.value,
        source_name=source.source_name,
        source_category=source.source_category,
        headline=source.headline,
        author=source.author,
        canonical_url=source.canonical_url,
        original_url=source.original_url,
        published_at=source.published_at,
        received_at=source.received_at,
        relationship=relationship,
        excerpt=excerpt,
        symbols=symbols,
    )


@router.get("/events", response_model=EventListResponse, summary="List canonical events")
async def list_events(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    event_status: Annotated[EventStatus | None, Query(alias="status")] = None,
    provider: SourceProvider | None = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    min_importance: Annotated[float | None, Query(ge=0, le=1)] = None,
    since_hours: Annotated[int | None, Query(ge=1, le=24 * 30)] = None,
) -> EventListResponse:
    filters = EventFilters(
        status=event_status,
        provider=provider,
        search=search,
        min_importance=min_importance,
        since=(dt.datetime.now(dt.UTC) - dt.timedelta(hours=since_hours) if since_hours else None),
        limit=limit,
        offset=offset,
    )
    repository = EventRepository(session)
    total = await repository.count(filters)
    events = await repository.list_events(filters)
    summary = await repository.source_summary([event.id for event in events])

    return EventListResponse(
        total=total,
        limit=limit,
        offset=offset,
        events=[_to_summary(event, *summary.get(event.id, (0, [], None))) for event in events],
    )


@router.get(
    "/events/{event_id}",
    response_model=EventDetailResponse,
    summary="One event with all of its evidence",
)
async def get_event(event_id: uuid.UUID, session: DbSession) -> EventDetailResponse:
    repository = EventRepository(session)
    event = await repository.get(event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")

    sources = await repository.sources_for_event(event_id)
    summary = await repository.source_summary([event_id])
    return EventDetailResponse(
        event=_to_summary(event, *summary.get(event_id, (0, [], None))),
        sources=[_to_source(source, relationship) for source, relationship in sources],
    )
