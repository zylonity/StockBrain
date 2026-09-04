"""End-to-end ingestion and deduplication against a real database.

The acceptance criterion for this phase is "duplicates do not multiply". These
tests exercise every deduplication layer through the real service, including the
concurrent case that only the database can decide.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
import sqlalchemy as sa

from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.db.models.system import AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import (
    EventSourceRelationship,
    EventStatus,
    SourceCategory,
    SourceProvider,
)
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.ingestion.service import IngestionOutcome, IngestionService

pytestmark = pytest.mark.integration


def _article(
    *,
    item_id: str | None = "alpaca-1",
    url: str | None = "https://www.benzinga.com/news/acme",
    headline: str | None = "Acme Wins Data Centre Contract",
    body: str | None = "<p>Acme signed a large contract.</p>",
    provider: SourceProvider = SourceProvider.ALPACA,
) -> RawSourceDocument:
    return RawSourceDocument(
        provider=provider,
        provider_item_id=item_id,
        url=url,
        source_name="Benzinga",
        source_category=SourceCategory.NEWSWIRE,
        headline=headline,
        published_at=dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.UTC),
        body=body,
        symbols=["ACME"],
        raw_payload={"id": item_id},
    )


async def _count(database: Database, model: type) -> int:
    async with database.session() as session:
        return int(
            (await session.execute(sa.select(sa.func.count()).select_from(model))).scalar_one()
        )


async def test_first_article_creates_a_source_and_an_event(clean_tables: Database) -> None:
    service = IngestionService(clean_tables)
    result = await service.ingest(_article())

    assert result.outcome is IngestionOutcome.CREATED_EVENT
    assert result.source_id is not None
    assert result.event_id is not None

    async with clean_tables.session() as session:
        source = await session.get(Source, result.source_id)
        assert source is not None
        # Tracking parameters stripped, host normalised.
        assert source.canonical_url == "https://benzinga.com/news/acme"
        assert source.normalized_text == "Acme signed a large contract."
        assert source.provider_metadata["symbols"] == ["ACME"]
        # The raw provider payload is preserved for audit.
        assert source.provider_metadata["raw_payload"] == {"id": "alpaca-1"}

        event = await session.get(Event, result.event_id)
        assert event is not None
        assert event.status is EventStatus.NEW
        assert event.title == "Acme Wins Data Centre Contract"

        link = (
            await session.execute(
                sa.select(EventSourceLink).where(EventSourceLink.event_id == result.event_id)
            )
        ).scalar_one()
        assert link.relationship_type is EventSourceRelationship.PRIMARY


async def test_layer_1_same_provider_item_id_is_a_duplicate(clean_tables: Database) -> None:
    service = IngestionService(clean_tables)
    await service.ingest(_article())
    # Same article re-delivered with a rewritten headline and a different URL.
    result = await service.ingest(
        _article(url="https://other.example.com/x", headline="Totally different wording")
    )

    assert result.outcome is IngestionOutcome.DUPLICATE_SOURCE
    assert result.detail == "PROVIDER_ITEM_ID"
    assert await _count(clean_tables, Source) == 1
    assert await _count(clean_tables, Event) == 1


async def test_layer_2_same_canonical_url_is_a_duplicate(clean_tables: Database) -> None:
    service = IngestionService(clean_tables)
    await service.ingest(_article(item_id=None))
    result = await service.ingest(
        _article(
            item_id=None,
            url="https://benzinga.com/news/acme/?utm_source=twitter&fbclid=abc#top",
        )
    )

    assert result.outcome is IngestionOutcome.DUPLICATE_SOURCE
    assert result.detail == "CANONICAL_URL"
    assert await _count(clean_tables, Source) == 1


async def test_layer_3_identical_content_at_a_different_url_is_a_duplicate(
    clean_tables: Database,
) -> None:
    """Verbatim syndication: same wire story, different outlet URL."""
    service = IngestionService(clean_tables)
    await service.ingest(_article(item_id=None))
    result = await service.ingest(
        _article(item_id=None, url="https://finance.example.com/wire/12345")
    )

    assert result.outcome is IngestionOutcome.DUPLICATE_SOURCE
    assert result.detail == "CONTENT_HASH"
    assert await _count(clean_tables, Source) == 1


async def test_layer_3_5_same_headline_different_body_links_to_one_event(
    clean_tables: Database,
) -> None:
    """Two outlets, same story, differently worded bodies: two sources, one event."""
    service = IngestionService(clean_tables)
    first = await service.ingest(_article(item_id="a-1"))
    second = await service.ingest(
        _article(
            item_id="a-2",
            url="https://reuters.com/business/acme",
            body="<p>A different rendering of the same news.</p>",
        )
    )

    assert second.outcome is IngestionOutcome.LINKED_TO_EVENT
    assert second.event_id == first.event_id
    assert await _count(clean_tables, Source) == 2
    assert await _count(clean_tables, Event) == 1

    async with clean_tables.session() as session:
        links = list(
            (
                await session.execute(
                    sa.select(EventSourceLink).where(EventSourceLink.event_id == first.event_id)
                )
            ).scalars()
        )
        relationships = sorted(link.relationship_type.value for link in links)
        assert relationships == ["CORROBORATING", "PRIMARY"]


async def test_genuinely_different_stories_stay_separate(clean_tables: Database) -> None:
    """The dedupe must not be so eager that distinct events get merged."""
    service = IngestionService(clean_tables)
    await service.ingest(_article(item_id="a-1", headline="Acme Wins Contract"))
    await service.ingest(
        _article(
            item_id="a-2",
            url="https://reuters.com/x",
            headline="Beta Corp Cuts Guidance",
            body="<p>Unrelated news.</p>",
        )
    )
    assert await _count(clean_tables, Event) == 2


async def test_event_grouping_expires_outside_the_match_window(
    clean_tables: Database,
) -> None:
    """A recurring headline months later is a new event, not the old one."""
    service = IngestionService(clean_tables, event_match_window=dt.timedelta(seconds=0))
    await service.ingest(_article(item_id="a-1"))
    result = await service.ingest(
        _article(item_id="a-2", url="https://reuters.com/x", body="<p>Different body.</p>")
    )
    assert result.outcome is IngestionOutcome.CREATED_EVENT
    assert await _count(clean_tables, Event) == 2


async def test_concurrent_ingestion_of_the_same_article_creates_one_source(
    clean_tables: Database,
) -> None:
    """The unique index, not a Python check, is what makes this safe."""
    service = IngestionService(clean_tables)
    results = await asyncio.gather(*(service.ingest(_article(item_id="race-1")) for _ in range(6)))

    created = [r for r in results if r.outcome is IngestionOutcome.CREATED_EVENT]
    assert len(created) == 1, [r.outcome.value for r in results]
    assert await _count(clean_tables, Source) == 1
    assert await _count(clean_tables, Event) == 1


async def test_ingestion_writes_an_audit_record(clean_tables: Database) -> None:
    service = IngestionService(clean_tables)
    result = await service.ingest(_article())

    async with clean_tables.session() as session:
        entries = list(
            (
                await session.execute(
                    sa.select(AuditLog).where(AuditLog.entity_id == result.event_id)
                )
            ).scalars()
        )
    assert [entry.action for entry in entries] == ["EVENT_CREATED"]
    assert entries[0].details["provider"] == "ALPACA"
    assert entries[0].details["canonical_url"] == "https://benzinga.com/news/acme"


async def test_a_source_without_a_headline_still_ingests(clean_tables: Database) -> None:
    service = IngestionService(clean_tables)
    result = await service.ingest(_article(headline=None))
    assert result.outcome is IngestionOutcome.CREATED_EVENT

    async with clean_tables.session() as session:
        event = await session.get(Event, result.event_id)
        assert event is not None
        assert event.title == "(untitled source)"


async def test_sources_without_urls_do_not_collide(clean_tables: Database) -> None:
    """SEC filings and search results with no URL must not dedupe against each other."""
    service = IngestionService(clean_tables)
    for index in range(3):
        result = await service.ingest(
            _article(
                item_id=f"sec-{index}",
                url=None,
                provider=SourceProvider.SEC,
                headline=f"Filing {index}",
                body=f"<p>Filing body {index}</p>",
            )
        )
        assert result.outcome is IngestionOutcome.CREATED_EVENT
    assert await _count(clean_tables, Source) == 3


async def test_classification_is_not_enqueued_until_a_handler_exists(
    clean_tables: Database,
) -> None:
    """Creating jobs nothing can run would fill the queue with guaranteed failures."""
    from stockbrain.db.models.system import Job

    service = IngestionService(clean_tables, classification_enabled=False)
    await service.ingest(_article())
    assert await _count(clean_tables, Job) == 0

    service = IngestionService(clean_tables, classification_enabled=True)
    await service.ingest(
        _article(item_id="a-2", url="https://reuters.com/y", headline="Other", body="<p>x</p>")
    )
    async with clean_tables.session() as session:
        job = (await session.execute(sa.select(Job))).scalar_one()
    assert job.job_type == "CLASSIFY_EVENT"
    assert job.dedupe_key is not None and job.dedupe_key.startswith("classify:")


async def test_distinct_filings_are_never_merged_by_headline(
    clean_tables: Database,
) -> None:
    """Regression: five Form 4 filings collapsed into one event.

    Filing titles are templated, so headline grouping merged five genuinely
    different filings by five different insiders. A filing is the primary record
    of its own occurrence and must stay its own event.
    """
    service = IngestionService(clean_tables)
    accessions = [f"0001140361-26-0356{index:02d}" for index in range(5)]

    for accession in accessions:
        result = await service.ingest(
            RawSourceDocument(
                provider=SourceProvider.SEC,
                provider_item_id=accession,
                url=f"https://www.sec.gov/Archives/edgar/data/320193/{accession}.htm",
                source_name="SEC EDGAR",
                source_category=SourceCategory.REGULATOR,
                headline="Apple Inc. filed Form 4",
                body=f"Accession: {accession}",
                is_distinct_event=True,
            )
        )
        assert result.outcome is IngestionOutcome.CREATED_EVENT, accession

    assert await _count(clean_tables, Source) == 5
    assert await _count(clean_tables, Event) == 5


async def test_news_articles_are_still_grouped_by_headline(
    clean_tables: Database,
) -> None:
    """The flag must not disable grouping for the case it exists to serve."""
    service = IngestionService(clean_tables)
    first = await service.ingest(_article(item_id="n-1"))
    second = await service.ingest(
        _article(item_id="n-2", url="https://reuters.com/z", body="<p>Other wording.</p>")
    )
    assert second.outcome is IngestionOutcome.LINKED_TO_EVENT
    assert second.event_id == first.event_id
