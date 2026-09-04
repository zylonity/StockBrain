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
    CompanyImpactResponse,
    EventDetailResponse,
    EventListResponse,
    EventSummaryResponse,
    LlmCallResponse,
    LlmUsageResponse,
    SourceResponse,
)
from stockbrain.db.models.companies import EventCompanyImpact
from stockbrain.db.models.research import LlmCall
from stockbrain.db.models.sources import Event, Source
from stockbrain.db.repositories.events import EventFilters, EventRepository
from stockbrain.enums import EventStatus, SourceProvider

router = APIRouter(prefix="/api/v1", tags=["events"])

#: Excerpt length sent to the list/detail views. The full text stays in the
#: database for research; the browser does not need it to triage.
EXCERPT_CHARS = 1200


def _to_summary(
    event: Event,
    source_count: int,
    providers: list[str],
    category: str | None,
    company_count: int = 0,
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
        confidence_score=event.confidence_score,
        candidate_score=event.candidate_score,
        relevant_to_public_equities=event.relevant_to_public_equities,
        needs_corroboration=event.needs_corroboration,
        topics=list(event.topics or []),
        classified_at=event.classified_at,
        classifier_model=event.classifier_model,
        classifier_prompt_version=event.classifier_prompt_version,
        classifier_error=event.classifier_error,
        merged_into_event_id=event.merged_into_event_id,
        source_count=source_count,
        company_count=company_count,
        providers=providers,
        top_category=category,
    )


def _to_impact(impact: EventCompanyImpact) -> CompanyImpactResponse:
    return CompanyImpactResponse(
        id=impact.id,
        company_name_hint=impact.company_name_hint,
        ticker_hint=impact.ticker_hint,
        exchange_hint=impact.exchange_hint,
        direction=impact.direction.value,
        impact_path=impact.impact_path,
        relationship_type=impact.relationship_type,
        materiality_score=impact.materiality_score,
        confidence=impact.confidence,
        explanation=impact.explanation,
        resolved_company_id=impact.company_id,
        resolution_confidence=impact.resolution_confidence,
    )


def _to_llm_call(call: LlmCall) -> LlmCallResponse:
    """Expose the call record, never the provider's hidden reasoning."""
    return LlmCallResponse(
        id=call.id,
        purpose=call.purpose,
        provider=call.provider,
        model=call.model,
        prompt_version=call.prompt_version,
        thinking_enabled=call.thinking_enabled,
        succeeded=call.succeeded,
        used=call.used,
        attempt=call.attempt,
        retry_count=call.retry_count,
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        cached_input_tokens=call.cached_input_tokens,
        estimated_cost_usd=call.estimated_cost_usd,
        latency_ms=call.latency_ms,
        finish_reason=call.finish_reason,
        provider_request_id=call.provider_request_id,
        had_reasoning_content=call.had_reasoning_content,
        error_class=call.error_class,
        error=call.error,
        created_at=call.created_at,
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
    companies = await repository.impacts_for_event(event_id)
    usage = await repository.llm_usage_for_event(event_id)
    calls = await repository.llm_calls_for_event(event_id)

    # The structured rationale is part of the requested schema and is safe to
    # show. Any provider-side reasoning content is never stored or returned.
    rationale = None
    if isinstance(event.classifier_output, dict):
        raw_rationale = event.classifier_output.get("rationale")
        rationale = str(raw_rationale) if raw_rationale else None

    return EventDetailResponse(
        event=_to_summary(
            event, *summary.get(event_id, (0, [], None)), company_count=len(companies)
        ),
        sources=[_to_source(source, relationship) for source, relationship in sources],
        companies=[_to_impact(impact) for impact in companies],
        rationale=rationale,
        llm_usage=LlmUsageResponse(**usage),
        llm_calls=[_to_llm_call(call) for call in calls],
    )
