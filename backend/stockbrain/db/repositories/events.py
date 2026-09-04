"""Read-side queries for events and their sources.

Kept out of the route handlers so the same query can serve the REST API and,
later, the Telegram bot without either duplicating SQL.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import EventCompanyImpact
from stockbrain.db.models.research import LlmCall
from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.enums import EventStatus, SourceProvider

__all__ = ["EventFilters", "EventRepository"]


@dataclass(slots=True)
class EventFilters:
    status: EventStatus | None = None
    provider: SourceProvider | None = None
    search: str | None = None
    min_importance: float | None = None
    since: dt.datetime | None = None
    limit: int = 50
    offset: int = 0


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_query(self, filters: EventFilters) -> sa.Select[Any]:
        stmt = sa.select(Event)
        if filters.status is not None:
            stmt = stmt.where(Event.status == filters.status)
        if filters.min_importance is not None:
            stmt = stmt.where(Event.importance_score >= filters.min_importance)
        if filters.since is not None:
            stmt = stmt.where(Event.first_seen_at >= filters.since)
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            stmt = stmt.where(Event.title.ilike(pattern))
        if filters.provider is not None:
            stmt = stmt.where(
                sa.exists(
                    sa.select(1)
                    .select_from(EventSourceLink)
                    .join(Source, Source.id == EventSourceLink.source_id)
                    .where(
                        EventSourceLink.event_id == Event.id,
                        Source.provider == filters.provider,
                    )
                )
            )
        return stmt

    async def count(self, filters: EventFilters) -> int:
        stmt = self._base_query(filters).with_only_columns(sa.func.count(Event.id)).order_by(None)
        return int((await self._session.execute(stmt)).scalar_one())

    async def list_events(self, filters: EventFilters) -> list[Event]:
        stmt = (
            self._base_query(filters)
            .order_by(Event.first_seen_at.desc())
            .limit(max(1, min(filters.limit, 200)))
            .offset(max(0, filters.offset))
        )
        return list((await self._session.execute(stmt)).scalars())

    async def get(self, event_id: uuid.UUID) -> Event | None:
        return await self._session.get(Event, event_id)

    async def source_summary(
        self, event_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[int, list[str], str | None]]:
        """Return ``event_id -> (source_count, providers, best_category)``.

        One query for the whole page rather than one per row.
        """
        if not event_ids:
            return {}
        stmt = (
            sa.select(
                EventSourceLink.event_id,
                Source.provider,
                Source.source_category,
            )
            .join(Source, Source.id == EventSourceLink.source_id)
            .where(EventSourceLink.event_id.in_(event_ids))
        )
        rows = (await self._session.execute(stmt)).all()

        # Highest-authority category wins the badge shown on the list row.
        priority = ["REGULATOR", "GOVERNMENT", "ISSUER", "NEWSWIRE", "PRESS", "UNKNOWN"]
        collected: dict[uuid.UUID, tuple[int, set[str], set[str]]] = {}
        for event_id, provider, category in rows:
            count, providers, categories = collected.get(event_id, (0, set(), set()))
            providers.add(provider.value if hasattr(provider, "value") else str(provider))
            if category:
                categories.add(str(category))
            collected[event_id] = (count + 1, providers, categories)

        result: dict[uuid.UUID, tuple[int, list[str], str | None]] = {}
        for event_id, (count, providers, categories) in collected.items():
            best = next((c for c in priority if c in categories), None)
            result[event_id] = (count, sorted(providers), best)
        return result

    async def company_counts(self, event_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Impact counts for a page of events, in one query."""
        if not event_ids:
            return {}
        rows = (
            await self._session.execute(
                sa.select(EventCompanyImpact.event_id, sa.func.count())
                .where(EventCompanyImpact.event_id.in_(event_ids))
                .group_by(EventCompanyImpact.event_id)
            )
        ).all()
        return {event_id: int(count) for event_id, count in rows}

    async def impacts_for_event(self, event_id: uuid.UUID) -> list[EventCompanyImpact]:
        stmt = (
            sa.select(EventCompanyImpact)
            .where(EventCompanyImpact.event_id == event_id)
            .order_by(EventCompanyImpact.materiality_score.desc())
        )
        return list((await self._session.execute(stmt)).scalars())

    async def llm_calls_for_event(self, event_id: uuid.UUID) -> list[LlmCall]:
        stmt = (
            sa.select(LlmCall)
            .where(LlmCall.event_id == event_id)
            .order_by(LlmCall.created_at.asc())
            .limit(50)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def llm_usage_for_event(self, event_id: uuid.UUID) -> dict[str, Any]:
        row = (
            await self._session.execute(
                sa.select(
                    sa.func.count(),
                    sa.func.coalesce(sa.func.sum(LlmCall.input_tokens), 0),
                    sa.func.coalesce(sa.func.sum(LlmCall.output_tokens), 0),
                    sa.func.coalesce(sa.func.sum(LlmCall.cached_input_tokens), 0),
                    sa.func.coalesce(sa.func.sum(LlmCall.estimated_cost_usd), 0),
                ).where(LlmCall.event_id == event_id)
            )
        ).one()
        return {
            "calls": int(row[0]),
            "input_tokens": int(row[1]),
            "output_tokens": int(row[2]),
            "cached_input_tokens": int(row[3]),
            "estimated_cost_usd": row[4],
        }

    async def sources_for_event(self, event_id: uuid.UUID) -> list[tuple[Source, str]]:
        stmt = (
            sa.select(Source, EventSourceLink.relationship_type)
            .join(EventSourceLink, EventSourceLink.source_id == Source.id)
            .where(EventSourceLink.event_id == event_id)
            .order_by(Source.published_at.desc().nullslast(), Source.received_at.desc())
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            (source, relationship.value if hasattr(relationship, "value") else str(relationship))
            for source, relationship in rows
        ]

    async def ingestion_stats(self) -> dict[str, Any]:
        day_ago = utcnow() - dt.timedelta(days=1)

        sources_total = int(
            (
                await self._session.execute(sa.select(sa.func.count()).select_from(Source))
            ).scalar_one()
        )
        events_total = int(
            (
                await self._session.execute(sa.select(sa.func.count()).select_from(Event))
            ).scalar_one()
        )
        sources_day = int(
            (
                await self._session.execute(
                    sa.select(sa.func.count())
                    .select_from(Source)
                    .where(Source.received_at >= day_ago)
                )
            ).scalar_one()
        )
        events_day = int(
            (
                await self._session.execute(
                    sa.select(sa.func.count())
                    .select_from(Event)
                    .where(Event.first_seen_at >= day_ago)
                )
            ).scalar_one()
        )
        by_status = {
            str(status.value if hasattr(status, "value") else status): int(count)
            for status, count in (
                await self._session.execute(
                    sa.select(Event.status, sa.func.count()).group_by(Event.status)
                )
            ).all()
        }
        by_provider = {
            str(provider.value if hasattr(provider, "value") else provider): int(count)
            for provider, count in (
                await self._session.execute(
                    sa.select(Source.provider, sa.func.count()).group_by(Source.provider)
                )
            ).all()
        }
        latest = (
            await self._session.execute(sa.select(sa.func.max(Source.received_at)))
        ).scalar_one_or_none()

        return {
            "sources_total": sources_total,
            "events_total": events_total,
            "sources_last_24h": sources_day,
            "events_last_24h": events_day,
            "events_by_status": by_status,
            "sources_by_provider": by_provider,
            "latest_source_at": latest,
        }
