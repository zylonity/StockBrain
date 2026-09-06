"""The extraction stage: triage order, and when a paid fallback may run.

Two things are asserted here that nothing else can assert, because both are
properties of the *sequence* rather than of any one component:

1. **Nothing is fetched before triage.**  Search results are stored as metadata,
   deduplicated and classified, and only an event the classifier promoted to
   ``CANDIDATE`` earns a page fetch.  Spending before triage is what a
   two-stage model exists to prevent, and it is the shape of the Phase 2
   incident.
2. **The paid fallback is the exception, not the path.**  Local extraction is
   free and runs first.  Firecrawl runs only when the local attempt failed for a
   reason a different fetcher could fix, the operator has switched it on twice,
   the budget grants a reservation, and the URL has not been tried before --
   and then exactly once.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.db.models.system import Job, ProviderCall
from stockbrain.db.session import Database
from stockbrain.enums import (
    EventSourceRelationship,
    EventStatus,
    ExtractionMethod,
    JobType,
    ProviderCallKind,
    SourceProvider,
)
from stockbrain.extraction.base import ExtractionFailure, ExtractionResult
from stockbrain.ingestion.service import IngestionService
from stockbrain.jobs.handlers import handle_content_extract
from stockbrain.jobs.registry import HandlerContext
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.services import ServiceContainer

pytestmark = pytest.mark.integration

URL = "https://reuters.com/article-worth-reading"


def _settings(database: Database, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "web_auth_enabled": False,
        "database_url": database.engine.url.render_as_string(hide_password=False),
        "brave_api_key": "brv-test",
        "discovery_enabled": True,
        "alpaca_news_enabled": False,
        "sec_enabled": False,
        "t212_metadata_enabled": False,
        "research_enabled": False,
        "classifier_enabled": False,
        "proposals_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _container(database: Database, **overrides: object) -> ServiceContainer:
    return ServiceContainer(
        settings=_settings(database, **overrides),
        database=database,
        health=ProviderHealthRegistry(),
    )


class RecordingExtractor:
    """A local extractor whose answer the test chooses."""

    name = "local"
    method = ExtractionMethod.LOCAL

    def __init__(self, result: ExtractionResult) -> None:
        self._result = result
        self.calls: list[str] = []

    async def extract(self, url: str) -> ExtractionResult:
        self.calls.append(url)
        return self._result

    async def aclose(self) -> None:
        return None


class RecordingFallback(RecordingExtractor):
    name = "firecrawl"
    method = ExtractionMethod.FIRECRAWL


def _ok(
    text: str = "A" * 800, method: ExtractionMethod = ExtractionMethod.LOCAL
) -> ExtractionResult:
    return ExtractionResult(method=method, url=URL, text=text, status_code=200)


def _failed(failure: ExtractionFailure) -> ExtractionResult:
    return ExtractionResult(
        method=ExtractionMethod.LOCAL, url=URL, failure=failure, detail="x", status_code=403
    )


async def _seed_source(
    database: Database, *, status: EventStatus = EventStatus.CANDIDATE, slug: str = ""
) -> uuid.UUID:
    """One Brave-discovered source holding a snippet, on an event in ``status``.

    ``slug`` distinguishes rows within one test: ``uq_sources_canonical_url_hash``
    is a real constraint and two identical seeds would collide on it -- which is
    exactly the property it exists to have.
    """
    url = f"{URL}{slug}"
    async with database.transaction() as session:
        source = Source(
            provider=SourceProvider.BRAVE,
            canonical_url=url,
            original_url=url,
            headline=f"Utility orders 400 transformers{slug}",
            content_hash=hashlib.sha256(url.encode()).hexdigest(),
            raw_content="a short snippet",
            normalized_text="a short snippet",
            discovered_by=[SourceProvider.BRAVE.value],
        )
        session.add(source)
        event = Event(
            title=f"Utility orders 400 transformers{slug}",
            title_hash=hashlib.sha256(f"t{slug}".encode()).hexdigest(),
            status=status,
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
        return source.id


def _context(
    services: ServiceContainer, database: Database, source_id: uuid.UUID
) -> HandlerContext:
    return HandlerContext(
        job_id=uuid.uuid4(),
        job_type=JobType.CONTENT_EXTRACT.value,
        payload={"source_id": str(source_id)},
        attempt=1,
        max_attempts=1,
        database=database,
        services=services,
    )


async def _source(database: Database, source_id: uuid.UUID) -> Source:
    async with database.session() as session:
        source = await session.get(Source, source_id)
        assert source is not None
        return source


# ---------------------------------------------------------------------------
# Triage order: nothing is fetched before the classifier has spoken
# ---------------------------------------------------------------------------
async def test_only_a_shortlisted_source_is_offered_for_extraction(
    clean_tables: Database,
) -> None:
    """The cheap-first pipeline, asserted at its one expensive junction.

    A source whose event is still ``NEW`` has not been triaged. Fetching its
    page would be spending -- bandwidth always, credits sometimes -- on
    something nothing has judged worth reading.
    """
    await _seed_source(clean_tables, status=EventStatus.NEW, slug="-new")
    ingestion = IngestionService(clean_tables)
    assert await ingestion.enqueue_content_fetches(limit=10) == 0

    await _seed_source(clean_tables, status=EventStatus.CANDIDATE, slug="-candidate")
    assert await ingestion.enqueue_content_fetches(limit=10) == 1

    async with clean_tables.session() as session:
        jobs = list((await session.execute(sa.select(Job))).scalars())
    assert [job.job_type for job in jobs] == [JobType.CONTENT_EXTRACT.value]
    # One attempt: the sweep offers it again next tick if it is still wanted,
    # and that path re-checks the budget.
    assert jobs[0].max_attempts == 1


async def test_a_provider_that_delivers_its_own_body_is_never_offered(
    clean_tables: Database,
) -> None:
    """An Alpaca article and an SEC filing arrive with the body attached.

    Re-fetching their URL would store a second copy of something already held,
    and would make an outbound request to a publisher for no reason.
    """
    async with clean_tables.transaction() as session:
        source = Source(
            provider=SourceProvider.ALPACA,
            canonical_url="https://benzinga.com/a",
            content_hash="2" * 64,
            raw_content="the full article body",
        )
        session.add(source)
        event = Event(title="t", title_hash="3" * 64, status=EventStatus.CANDIDATE)
        session.add(event)
        await session.flush()
        session.add(EventSourceLink(event_id=event.id, source_id=source.id))

    assert await IngestionService(clean_tables).enqueue_content_fetches(limit=10) == 0


async def test_a_source_that_already_has_a_body_is_not_offered_again(
    clean_tables: Database,
) -> None:
    source_id = await _seed_source(clean_tables)
    ingestion = IngestionService(clean_tables)
    await ingestion.attach_fetched_content(
        source_id, body="the article", method=ExtractionMethod.LOCAL
    )
    assert await ingestion.enqueue_content_fetches(limit=10) == 0


# ---------------------------------------------------------------------------
# Local first, and usually only
# ---------------------------------------------------------------------------
async def test_a_successful_local_extraction_never_reaches_firecrawl(
    clean_tables: Database,
) -> None:
    """The normal path, and it costs nothing."""
    source_id = await _seed_source(clean_tables)
    services = _container(
        clean_tables,
        firecrawl_api_key="fc-test",
        firecrawl_enabled=True,
        firecrawl_fallback_extraction_enabled=True,
    )
    local = RecordingExtractor(_ok())
    fallback = RecordingFallback(_ok(method=ExtractionMethod.FIRECRAWL))
    services.content_extractor = local
    services.firecrawl_extractor = fallback  # type: ignore[assignment]

    await handle_content_extract(_context(services, clean_tables, source_id))

    assert local.calls == [URL]
    assert fallback.calls == []
    source = await _source(clean_tables, source_id)
    assert source.extraction_method == ExtractionMethod.LOCAL.value
    assert source.content_fetched_at is not None
    assert source.raw_content is not None
    assert "AAA" in source.raw_content
    # And nothing was reserved against any budget.
    async with clean_tables.session() as session:
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(ProviderCall))
        ).scalar_one() == 0


async def test_a_local_failure_the_fallback_cannot_fix_does_not_spend(
    clean_tables: Database,
) -> None:
    """A refused URL or a PDF does not become extractable by paying someone
    else to fetch it.

    Spending on a failure the fallback will reproduce is precisely how a
    fallback becomes a cost storm.
    """
    for failure in (ExtractionFailure.REFUSED, ExtractionFailure.UNSUPPORTED_CONTENT_TYPE):
        source_id = await _seed_source(clean_tables, slug=f"-{failure.value}")
        services = _container(
            clean_tables,
            firecrawl_api_key="fc-test",
            firecrawl_enabled=True,
            firecrawl_fallback_extraction_enabled=True,
        )
        fallback = RecordingFallback(_ok(method=ExtractionMethod.FIRECRAWL))
        services.content_extractor = RecordingExtractor(_failed(failure))
        services.firecrawl_extractor = fallback  # type: ignore[assignment]

        await handle_content_extract(_context(services, clean_tables, source_id))
        assert fallback.calls == [], failure

    async with clean_tables.session() as session:
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(ProviderCall))
        ).scalar_one() == 0


@pytest.mark.parametrize(
    "failure",
    [
        ExtractionFailure.HTTP_ERROR,
        ExtractionFailure.TRANSPORT,
        ExtractionFailure.INSUFFICIENT_TEXT,
    ],
)
async def test_a_fixable_local_failure_earns_exactly_one_paid_attempt(
    clean_tables: Database, failure: ExtractionFailure
) -> None:
    """A 403 to a plain client is exactly what a rendering scraper is for.

    One attempt, one reservation, one ledger row -- and no retry, because
    Firecrawl bills for a request its infrastructure processed even when the
    target answered an error.
    """
    source_id = await _seed_source(clean_tables)
    services = _container(
        clean_tables,
        firecrawl_api_key="fc-test",
        firecrawl_enabled=True,
        firecrawl_fallback_extraction_enabled=True,
    )
    fallback = RecordingFallback(_ok(method=ExtractionMethod.FIRECRAWL))
    services.content_extractor = RecordingExtractor(_failed(failure))
    services.firecrawl_extractor = fallback  # type: ignore[assignment]

    await handle_content_extract(_context(services, clean_tables, source_id))

    assert fallback.calls == [URL]
    source = await _source(clean_tables, source_id)
    assert source.extraction_method == ExtractionMethod.FIRECRAWL.value

    async with clean_tables.session() as session:
        rows = list((await session.execute(sa.select(ProviderCall))).scalars())
    assert len(rows) == 1
    assert rows[0].provider == "firecrawl"
    assert rows[0].kind is ProviderCallKind.SCRAPE
    assert rows[0].units_charged == 1
    assert rows[0].target_url == URL


async def test_the_fallback_is_off_unless_both_switches_are_on(
    clean_tables: Database,
) -> None:
    """ "The credential exists" and "spend it on this page" are two decisions."""
    combinations: list[dict[str, object]] = [
        {"firecrawl_api_key": "fc-test"},
        {"firecrawl_api_key": "fc-test", "firecrawl_enabled": True},
        {"firecrawl_api_key": "fc-test", "firecrawl_fallback_extraction_enabled": True},
    ]
    for index, overrides in enumerate(combinations):
        source_id = await _seed_source(clean_tables, slug=f"-switch{index}")
        services = _container(clean_tables, **overrides)
        services.content_extractor = RecordingExtractor(_failed(ExtractionFailure.HTTP_ERROR))
        assert services.firecrawl_extractor is None, overrides

        await handle_content_extract(_context(services, clean_tables, source_id))
        source = await _source(clean_tables, source_id)
        # The attempt is still recorded, so nothing tries the same URL again.
        assert source.content_fetched_at is not None
        assert source.extraction_method == ExtractionMethod.NONE.value

    async with clean_tables.session() as session:
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(ProviderCall))
        ).scalar_one() == 0


async def test_an_exhausted_firecrawl_budget_refuses_the_fallback(
    clean_tables: Database,
) -> None:
    """The budget is the last gate and it is not advisory."""
    source_id = await _seed_source(clean_tables)
    services = _container(
        clean_tables,
        firecrawl_api_key="fc-test",
        firecrawl_enabled=True,
        firecrawl_fallback_extraction_enabled=True,
        firecrawl_max_scrapes_per_day=0,
    )
    fallback = RecordingFallback(_ok(method=ExtractionMethod.FIRECRAWL))
    services.content_extractor = RecordingExtractor(_failed(ExtractionFailure.HTTP_ERROR))
    services.firecrawl_extractor = fallback  # type: ignore[assignment]

    await handle_content_extract(_context(services, clean_tables, source_id))
    assert fallback.calls == []


async def test_a_second_job_for_the_same_source_pays_nothing(
    clean_tables: Database,
) -> None:
    """**At most one paid attempt per URL.**

    A redelivered job, a duplicate enqueue or an operator re-running the sweep
    must not buy the same page twice. ``content_fetched_at`` is written even
    when the attempt produced nothing, which is what makes that true rather
    than aspirational.
    """
    source_id = await _seed_source(clean_tables)
    services = _container(
        clean_tables,
        firecrawl_api_key="fc-test",
        firecrawl_enabled=True,
        firecrawl_fallback_extraction_enabled=True,
    )
    local = RecordingExtractor(_failed(ExtractionFailure.HTTP_ERROR))
    fallback = RecordingFallback(_failed(ExtractionFailure.TRANSPORT))
    services.content_extractor = local
    services.firecrawl_extractor = fallback  # type: ignore[assignment]

    context = _context(services, clean_tables, source_id)
    await handle_content_extract(context)
    await handle_content_extract(context)
    await handle_content_extract(context)

    assert local.calls == [URL]
    assert fallback.calls == [URL]
    async with clean_tables.session() as session:
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(ProviderCall))
        ).scalar_one() == 1


async def test_a_failed_fallback_is_charged_and_not_retried(
    clean_tables: Database,
) -> None:
    """Firecrawl charges for a request its infrastructure processed even when
    the target answered an error, so the reservation stands."""
    source_id = await _seed_source(clean_tables)
    services = _container(
        clean_tables,
        firecrawl_api_key="fc-test",
        firecrawl_enabled=True,
        firecrawl_fallback_extraction_enabled=True,
    )
    services.content_extractor = RecordingExtractor(_failed(ExtractionFailure.HTTP_ERROR))
    services.firecrawl_extractor = RecordingFallback(  # type: ignore[assignment]
        _failed(ExtractionFailure.TRANSPORT)
    )

    await handle_content_extract(_context(services, clean_tables, source_id))

    async with clean_tables.session() as session:
        rows = list((await session.execute(sa.select(ProviderCall))).scalars())
    assert len(rows) == 1
    assert rows[0].units_charged == 1
    # The exception category, never a provider body: an error body can echo the
    # request, and the request carries an Authorization header.
    assert rows[0].error_category == ExtractionFailure.TRANSPORT.value


async def test_an_empty_extraction_leaves_the_snippet_in_place(
    clean_tables: Database,
) -> None:
    """A failed fetch must not replace evidence with nothing.

    The row keeps what the search provider handed over, and records that the
    attempt was made.
    """
    source_id = await _seed_source(clean_tables)
    services = _container(clean_tables)
    services.content_extractor = RecordingExtractor(_failed(ExtractionFailure.INSUFFICIENT_TEXT))

    await handle_content_extract(_context(services, clean_tables, source_id))
    source = await _source(clean_tables, source_id)
    assert source.raw_content == "a short snippet"
    assert source.extraction_method == ExtractionMethod.NONE.value
    assert source.provider_metadata["content_fetch"]["stored"] is False


async def test_extraction_does_not_rewrite_the_dedupe_identity(
    clean_tables: Database,
) -> None:
    """``content_hash`` is the row's identity for deduplication.

    Rewriting it after a fetch would make the same article ingestable a second
    time, because the URL/hash unique index would stop colliding.
    """
    source_id = await _seed_source(clean_tables)
    before = (await _source(clean_tables, source_id)).content_hash

    services = _container(clean_tables)
    services.content_extractor = RecordingExtractor(_ok())
    await handle_content_extract(_context(services, clean_tables, source_id))

    assert (await _source(clean_tables, source_id)).content_hash == before


# ---------------------------------------------------------------------------
# Cross-provider provenance
# ---------------------------------------------------------------------------
async def test_the_same_url_from_two_providers_is_one_source_with_two_names(
    clean_tables: Database,
) -> None:
    """Brave and Exa both surfacing one page is corroboration, not two events.

    The second provider is appended to the existing row's provenance: no new
    source, no new event, no second classification, and nothing extra to read.
    """
    from stockbrain.enums import WebDiscoveryKind
    from stockbrain.ingestion.service import IngestionOutcome
    from stockbrain.ingestion.web_search import WebSearchResult, to_raw_document

    ingestion = IngestionService(clean_tables)
    shared: dict[str, Any] = {
        "url": URL,
        "title": "Utility orders 400 transformers",
        "snippet": "A regional utility has placed an order.",
    }

    first = await ingestion.ingest(
        to_raw_document(
            WebSearchResult(provider=SourceProvider.BRAVE, **shared),
            discovery_query="transformer backlog",
            kind=WebDiscoveryKind.ROUTINE,
        )
    )
    second = await ingestion.ingest(
        to_raw_document(
            WebSearchResult(provider=SourceProvider.EXA, **shared),
            discovery_query="who benefits from a transformer shortage",
            kind=WebDiscoveryKind.SEMANTIC,
        )
    )

    assert first.outcome is IngestionOutcome.CREATED_EVENT
    assert second.outcome is IngestionOutcome.DUPLICATE_SOURCE
    assert second.source_id == first.source_id

    async with clean_tables.session() as session:
        sources = list((await session.execute(sa.select(Source))).scalars())
        events = list((await session.execute(sa.select(Event))).scalars())
    assert len(sources) == 1
    assert len(events) == 1
    # Who found it first still owns the row's identity; the second is appended.
    assert sources[0].provider is SourceProvider.BRAVE
    assert sources[0].discovered_by == ["BRAVE", "EXA"]


async def test_a_third_sighting_by_a_known_provider_adds_nothing(
    clean_tables: Database,
) -> None:
    """Ordered and de-duplicated, so the list reads as a history rather than
    growing without bound."""
    from stockbrain.enums import WebDiscoveryKind
    from stockbrain.ingestion.web_search import WebSearchResult, to_raw_document

    ingestion = IngestionService(clean_tables)
    document = to_raw_document(
        WebSearchResult(provider=SourceProvider.BRAVE, url=URL, title="t", snippet="s"),
        discovery_query="q",
        kind=WebDiscoveryKind.ROUTINE,
    )
    for _ in range(3):
        await ingestion.ingest(document)

    async with clean_tables.session() as session:
        sources = list((await session.execute(sa.select(Source))).scalars())
    assert len(sources) == 1
    assert sources[0].discovered_by == ["BRAVE"]
