"""Research packets read compact, point-in-time outcomes from persisted bars."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.db.models.companies import EventCompanyImpact
from stockbrain.db.models.news_history import NewsHistoryCandle
from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.db.session import Database
from stockbrain.enums import (
    EventSourceRelationship,
    EventStatus,
    ImpactDirection,
    ResolutionStatus,
    SourceProvider,
)
from stockbrain.intelligence.news_outcome_research import NewsOutcomeResearchProvider
from tests.integration.test_research import seed

pytestmark = pytest.mark.integration


async def _prior_event(db: Database, value: object, *, published: dt.datetime) -> uuid.UUID:
    from stockbrain.intelligence.research import ResearchPacket

    assert isinstance(value, ResearchPacket)
    event_id, source_a, source_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with db.transaction() as session:
        session.add(
            Event(
                id=event_id,
                event_type="CAPACITY",
                title="Earlier capacity story",
                title_hash="c" * 64,
                first_seen_at=published + dt.timedelta(minutes=1),
                event_time=published,
                classified_at=published + dt.timedelta(minutes=3),
                status=EventStatus.CLASSIFIED,
                created_at=published,
                updated_at=published,
            )
        )
        await session.flush()
        for source_id in (source_a, source_b):
            session.add(
                Source(
                    id=source_id,
                    provider=SourceProvider.MANUAL,
                    content_hash=str(source_id).replace("-", "") * 2,
                    headline="Earlier capacity story",
                    published_at=published,
                    received_at=published + dt.timedelta(minutes=1),
                    created_at=published,
                    updated_at=published,
                )
            )
        await session.flush()
        for source_id in (source_a, source_b):
            session.add(
                EventSourceLink(
                    event_id=event_id,
                    source_id=source_id,
                    relationship_type=EventSourceRelationship.PRIMARY,
                    created_at=published + dt.timedelta(minutes=1),
                )
            )
        session.add(
            EventCompanyImpact(
                event_id=event_id,
                company_id=value.company.company_id,
                company_name_hint=value.company.name,
                company_key="apple-prior",
                direction=ImpactDirection.POSITIVE,
                materiality_score=0.7,
                confidence=0.8,
                broker_instrument_id=value.company.broker_instrument_id,
                resolution_status=ResolutionStatus.RESOLVED,
                created_at=published,
                updated_at=published,
            )
        )
    return event_id


async def _candles(db: Database, published: dt.datetime, *, ingested_at: dt.datetime) -> None:
    async with db.transaction() as session:
        for minute in range(-1, 67):
            moment = published + dt.timedelta(minutes=minute)
            for symbol, price in (("AAPL", Decimal("100")), ("SPY", Decimal("500"))):
                session.add(
                    NewsHistoryCandle(
                        symbol=symbol,
                        timeframe="1Min",
                        provider="fixture",
                        feed="sip",
                        bar_start=moment,
                        observed_at=moment + dt.timedelta(minutes=1),
                        ingested_at=ingested_at,
                        open=price,
                        high=price,
                        low=price,
                        close=price,
                        volume=100,
                    )
                )


async def test_packet_context_groups_sources_and_withholds_tiny_sample_stats(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    published = dt.datetime(2026, 9, 1, 14, tzinfo=dt.UTC)
    await _prior_event(clean_tables, value, published=published)
    await _candles(clean_tables, published, ingested_at=published + dt.timedelta(days=1))
    async with clean_tables.transaction() as session:
        current = await session.get(Event, value.event_id)
        assert current is not None
        current.event_type = "CAPACITY"
        current.classified_at = value.event_time

    data = await NewsOutcomeResearchProvider(clean_tables).context(value)
    payload = json.loads(data[0].text)
    assert payload["match"] == "same_company_and_event_type"
    assert payload["candidate_events"] == 1  # two articles, one event
    assert len(payload["examples"]) == 1
    assert payload["summary"]["1h"]["n"] == 1
    assert payload["summary"]["1h"]["median"] is None
    assert payload["status"] == "descriptive_only"
    assert payload["cache_coverage"]["AAPL"]["status"] == "unknown_at_as_of"
    assert len(data[0].text) < 4000


async def test_packet_context_excludes_candles_ingested_after_as_of(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    published = dt.datetime(2026, 9, 1, 14, tzinfo=dt.UTC)
    await _prior_event(clean_tables, value, published=published)
    await _candles(clean_tables, published, ingested_at=value.as_of + dt.timedelta(days=1))
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(Event)
            .where(Event.id == value.event_id)
            .values(event_type="CAPACITY", classified_at=value.event_time)
        )
    data = await NewsOutcomeResearchProvider(clean_tables).context(value)
    payload = json.loads(data[0].text)
    assert payload["candidate_events"] == 1
    assert payload["examples"] == []
    assert payload["excluded"]["candles_missing"] == 1


async def test_packet_context_excludes_links_created_after_as_of(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    published = dt.datetime(2026, 9, 1, 14, tzinfo=dt.UTC)
    prior_id = await _prior_event(clean_tables, value, published=published)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(EventSourceLink)
            .where(EventSourceLink.event_id == prior_id)
            .values(created_at=value.as_of + dt.timedelta(minutes=1))
        )
        await session.execute(
            sa.update(Event)
            .where(Event.id == value.event_id)
            .values(event_type="CAPACITY", classified_at=value.event_time)
        )
    data = await NewsOutcomeResearchProvider(clean_tables).context(value)
    payload = json.loads(data[0].text)
    assert payload["candidate_events"] == 0


async def test_packet_context_has_completed_one_and_five_session_outcomes(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    published = dt.datetime(2026, 8, 20, 14, tzinfo=dt.UTC)
    await _prior_event(clean_tables, value, published=published)
    await _candles(clean_tables, published, ingested_at=published + dt.timedelta(days=1))
    dates = (21, 24, 25, 26, 27)
    async with clean_tables.transaction() as session:
        current = await session.get(Event, value.event_id)
        assert current is not None
        current.event_type = "CAPACITY"
        current.classified_at = value.event_time
        for day in dates:
            moment = dt.datetime(2026, 8, day, 19, 59, tzinfo=dt.UTC)
            for symbol, price in (("AAPL", Decimal("100")), ("SPY", Decimal("500"))):
                session.add(NewsHistoryCandle(
                    symbol=symbol, timeframe="1Min", provider="fixture", feed="sip",
                    bar_start=moment, observed_at=moment + dt.timedelta(minutes=1),
                    ingested_at=dt.datetime(2026, 8, 28, tzinfo=dt.UTC),
                    open=price, high=price, low=price, close=price, volume=100,
                ))

    later = value.model_copy(update={"as_of": dt.datetime(2026, 9, 15, tzinfo=dt.UTC)})
    data = await NewsOutcomeResearchProvider(clean_tables).context(later)
    payload = json.loads(data[0].text)
    assert payload["summary"]["1d"]["n"] == 1
    assert payload["summary"]["5d"]["n"] == 1
