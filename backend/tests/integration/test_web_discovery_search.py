"""The search handler end to end: reserve, call, ingest, account, cool down.

One handler, two providers, and the assertion that matters most is negative:
**a search stores metadata and fetches nothing.**  Everything downstream of it
-- deduplication, the deterministic filters, the classifier -- runs on that
metadata, and only what survives earns a page fetch.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.sources import Event, Source
from stockbrain.db.models.system import DiscoveryQuery, DiscoveryTopic, ProviderCall
from stockbrain.db.session import Database
from stockbrain.enums import (
    JobType,
    ProviderCallKind,
    ProviderCallOutcome,
    ProviderStatus,
    SourceProvider,
    WebDiscoveryKind,
)
from stockbrain.errors import ProviderAuthError, ProviderRateLimited, ProviderUnavailable
from stockbrain.ingestion.web_search import WebSearchOutcome, WebSearchQuery, WebSearchResult
from stockbrain.jobs.handlers import handle_web_discovery_search
from stockbrain.jobs.registry import HandlerContext
from stockbrain.observability.health import ProviderHealthRegistry, ProviderName
from stockbrain.services import ServiceContainer

pytestmark = pytest.mark.integration


def _settings(database: Database, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "web_auth_enabled": False,
        "database_url": database.engine.url.render_as_string(hide_password=False),
        "brave_api_key": "brv-test",
        "exa_api_key": "exa-test",
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


class FakeProvider:
    """A search backend whose answer -- or failure -- the test chooses."""

    def __init__(
        self,
        provider: SourceProvider,
        kind: WebDiscoveryKind,
        *,
        outcome: WebSearchOutcome | None = None,
        error: Exception | None = None,
    ) -> None:
        self.name = provider.value.lower()
        self.provider = provider
        self.supported_kinds = frozenset({kind})
        self._outcome = outcome
        self._error = error
        self.queries: list[WebSearchQuery] = []

    async def search(self, query: WebSearchQuery) -> WebSearchOutcome:
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        assert self._outcome is not None
        return self._outcome

    async def aclose(self) -> None:
        return None


def _outcome(*urls: str, provider: SourceProvider = SourceProvider.BRAVE) -> WebSearchOutcome:
    return WebSearchOutcome(
        results=tuple(
            WebSearchResult(
                provider=provider,
                url=url,
                title=f"Headline for {url}",
                snippet="A snippet the classifier can triage.",
                published_at=dt.datetime(2026, 9, 4, tzinfo=dt.UTC),
                source_domain="reuters.com",
            )
            for url in urls
        ),
        results_returned=len(urls),
        billed_units_reported=1,
    )


async def _seed_query(
    database: Database, *, kind: WebDiscoveryKind = WebDiscoveryKind.ROUTINE
) -> uuid.UUID:
    async with database.transaction() as session:
        topic = DiscoveryTopic(
            slug="ai_infrastructure",
            name="AI infrastructure",
            enabled=True,
            interval_minutes=720,
            result_limit=10,
            freshness_days=7,
            include_domains=[],
            exclude_domains=[],
        )
        session.add(topic)
        await session.flush()
        query = DiscoveryQuery(
            topic_id=topic.id, query="grid transformer backlog", enabled=True, search_kind=kind
        )
        session.add(query)
        await session.flush()
        return query.id


def _container(database: Database, provider: Any = None, **overrides: object) -> ServiceContainer:
    services = ServiceContainer(
        settings=_settings(database, **overrides),
        database=database,
        health=ProviderHealthRegistry(),
    )
    if provider is not None:
        if provider.provider is SourceProvider.EXA:
            services.exa = provider
        else:
            services.brave = provider
    return services


def _context(services: ServiceContainer, database: Database, query_id: uuid.UUID) -> HandlerContext:
    return HandlerContext(
        job_id=uuid.uuid4(),
        job_type=JobType.WEB_DISCOVERY_SEARCH.value,
        payload={"query_id": str(query_id)},
        attempt=1,
        max_attempts=1,
        database=database,
        services=services,
    )


async def _rows(database: Database, model: Any) -> list[Any]:
    async with database.session() as session:
        return list((await session.execute(sa.select(model))).scalars())


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
async def test_a_search_stores_metadata_and_fetches_nothing(
    clean_tables: Database,
) -> None:
    """The single most important property of the two-stage model.

    Sources land with a snippet and ``content_fetched_at`` NULL. Fetching the
    article here -- which is what ``scrape_content`` used to do, on every
    result -- is the 20x cost multiplier that emptied the allowance.
    """
    query_id = await _seed_query(clean_tables)
    provider = FakeProvider(
        SourceProvider.BRAVE,
        WebDiscoveryKind.ROUTINE,
        outcome=_outcome("https://reuters.com/a", "https://cnbc.com/b"),
    )
    services = _container(clean_tables, provider)

    await handle_web_discovery_search(_context(services, clean_tables, query_id))

    sources = await _rows(clean_tables, Source)
    assert len(sources) == 2
    for source in sources:
        assert source.provider is SourceProvider.BRAVE
        assert source.content_fetched_at is None
        assert source.extraction_method is None
        assert source.raw_content == "A snippet the classifier can triage."
        assert source.discovered_by == ["BRAVE"]
    assert len(await _rows(clean_tables, Event)) == 2


async def test_the_search_is_reserved_before_it_is_made_and_reconciled_after(
    clean_tables: Database,
) -> None:
    """The reservation is committed before any socket is opened, so a process
    that dies mid-call leaves an over-estimate rather than an unaccounted
    spend."""
    query_id = await _seed_query(clean_tables)
    services = _container(
        clean_tables,
        FakeProvider(
            SourceProvider.BRAVE, WebDiscoveryKind.ROUTINE, outcome=_outcome("https://a.example/x")
        ),
    )

    await handle_web_discovery_search(_context(services, clean_tables, query_id))

    rows = await _rows(clean_tables, ProviderCall)
    assert len(rows) == 1
    assert rows[0].provider == "brave"
    assert rows[0].kind is ProviderCallKind.SEARCH
    assert rows[0].outcome is ProviderCallOutcome.SUCCEEDED
    assert rows[0].units_charged == 1
    assert rows[0].results_returned == 1
    # A search fetched no pages, and the row says so.
    assert rows[0].pages_scraped == 0
    assert rows[0].scrape_requested is False


async def test_a_successful_search_writes_a_durable_cooldown(
    clean_tables: Database,
) -> None:
    """Eligibility is a column, not a subtraction: a restart cannot reset it."""
    query_id = await _seed_query(clean_tables)
    services = _container(
        clean_tables,
        FakeProvider(
            SourceProvider.BRAVE, WebDiscoveryKind.ROUTINE, outcome=_outcome("https://a.example/x")
        ),
    )

    await handle_web_discovery_search(_context(services, clean_tables, query_id))

    query = (await _rows(clean_tables, DiscoveryQuery))[0]
    assert query.last_success_at is not None
    assert query.consecutive_failures == 0
    assert query.searches_performed == 1
    assert query.results_seen == 1
    assert query.next_eligible_at is not None
    assert query.next_eligible_at > dt.datetime.now(dt.UTC) + dt.timedelta(hours=11)
    assert services.health.get(ProviderName.BRAVE).status is ProviderStatus.HEALTHY


async def test_the_query_is_sent_with_the_topic_freshness_and_a_clamped_limit(
    clean_tables: Database,
) -> None:
    """Every ceiling is applied as a *reduction*.

    A topic row asking for a hundred results is a topic row asking for an
    overage line on every search.
    """
    query_id = await _seed_query(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(sa.update(DiscoveryTopic).values(result_limit=100))

    provider = FakeProvider(
        SourceProvider.BRAVE, WebDiscoveryKind.ROUTINE, outcome=_outcome("https://a.example/x")
    )
    services = _container(clean_tables, provider)
    await handle_web_discovery_search(_context(services, clean_tables, query_id))

    sent = provider.queries[0]
    assert sent.limit == 10
    assert sent.freshness_days == 7
    assert sent.kind is WebDiscoveryKind.ROUTINE


# ---------------------------------------------------------------------------
# Failure, and what it must not do
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ProviderAuthError("brave: rejected"), ProviderStatus.DOWN),
        (ProviderRateLimited("brave: 429"), ProviderStatus.DEGRADED),
        (ProviderUnavailable("brave: 503"), ProviderStatus.DEGRADED),
    ],
)
async def test_a_failed_search_degrades_one_provider_and_cools_the_query_down(
    clean_tables: Database, error: Exception, expected_status: ProviderStatus
) -> None:
    """Deliberately not re-raised.

    A job failure would add the queue's own retry on top of a call that may cost
    money each time it is tried; the durable cooldown is the retry.
    """
    query_id = await _seed_query(clean_tables)
    services = _container(
        clean_tables,
        FakeProvider(SourceProvider.BRAVE, WebDiscoveryKind.ROUTINE, error=error),
    )

    await handle_web_discovery_search(_context(services, clean_tables, query_id))

    query = (await _rows(clean_tables, DiscoveryQuery))[0]
    assert query.consecutive_failures == 1
    assert query.next_eligible_at is not None
    assert query.last_error is not None
    assert services.health.get(ProviderName.BRAVE).status is expected_status
    # No sources, no events: a failed search discovered nothing.
    assert await _rows(clean_tables, Source) == []


async def test_a_brave_rate_limit_costs_nothing_but_a_timeout_is_charged(
    clean_tables: Database,
) -> None:
    """Brave documents that only successful requests are billed.

    The refund is honoured for a *classified* provider error and withheld for a
    transport failure, because a timeout is exactly the case where nobody knows
    whether the far side served the request.
    """
    query_id = await _seed_query(clean_tables)
    services = _container(
        clean_tables,
        FakeProvider(
            SourceProvider.BRAVE, WebDiscoveryKind.ROUTINE, error=ProviderRateLimited("429")
        ),
    )
    await handle_web_discovery_search(_context(services, clean_tables, query_id))
    assert (await _rows(clean_tables, ProviderCall))[0].units_charged == 0

    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(DiscoveryQuery).values(next_eligible_at=sa.func.now() - dt.timedelta(days=1))
        )
    services = _container(
        clean_tables,
        FakeProvider(
            SourceProvider.BRAVE,
            WebDiscoveryKind.ROUTINE,
            error=ProviderUnavailable("brave: transport failure (ReadTimeout)"),
        ),
    )
    await handle_web_discovery_search(_context(services, clean_tables, query_id))

    rows = sorted(await _rows(clean_tables, ProviderCall), key=lambda r: r.reserved_at)
    assert [row.units_charged for row in rows] == [0, 1]


async def test_an_exa_failure_is_always_charged(clean_tables: Database) -> None:
    """Exa publishes no "failures are free" guarantee, and no provider is
    assumed generous without saying so in writing."""
    query_id = await _seed_query(clean_tables, kind=WebDiscoveryKind.SEMANTIC)
    services = _container(
        clean_tables,
        FakeProvider(
            SourceProvider.EXA, WebDiscoveryKind.SEMANTIC, error=ProviderRateLimited("429")
        ),
    )
    await handle_web_discovery_search(_context(services, clean_tables, query_id))

    rows = await _rows(clean_tables, ProviderCall)
    assert rows[0].provider == "exa"
    assert rows[0].units_charged == 1


async def test_an_exhausted_budget_is_a_deferral_not_a_failure(
    clean_tables: Database,
) -> None:
    """A refusal is a normal outcome: StockBrain declined to spend, the
    provider did not fail.  ``consecutive_failures`` stays at zero, so a
    budget-refused query is not treated as a broken one."""
    query_id = await _seed_query(clean_tables)
    provider = FakeProvider(
        SourceProvider.BRAVE, WebDiscoveryKind.ROUTINE, outcome=_outcome("https://a.example/x")
    )
    services = _container(
        clean_tables, provider, brave_max_searches_per_day=0, brave_max_searches_per_month=0
    )

    await handle_web_discovery_search(_context(services, clean_tables, query_id))

    assert provider.queries == []
    query = (await _rows(clean_tables, DiscoveryQuery))[0]
    assert query.consecutive_failures == 0
    assert query.next_eligible_at is not None
    assert services.health.get(ProviderName.BRAVE).status in (
        ProviderStatus.BUDGET_EXHAUSTED,
        ProviderStatus.DISABLED,
    )
    assert await _rows(clean_tables, ProviderCall) == []


async def test_an_unconfigured_provider_defers_rather_than_fanning_out(
    clean_tables: Database,
) -> None:
    """**No automatic fallback between paid providers.**

    Brave unconfigured means routine queries wait. It does not mean they get
    answered by Exa, which costs ten times as much -- that pattern is how one
    provider's outage becomes another provider's invoice.
    """
    query_id = await _seed_query(clean_tables)
    exa = FakeProvider(
        SourceProvider.EXA,
        WebDiscoveryKind.SEMANTIC,
        outcome=_outcome("https://a.example/x", provider=SourceProvider.EXA),
    )
    services = _container(clean_tables, exa, brave_api_key="")
    assert services.brave is None

    await handle_web_discovery_search(_context(services, clean_tables, query_id))

    assert exa.queries == []
    assert await _rows(clean_tables, ProviderCall) == []
    assert await _rows(clean_tables, Source) == []
    query = (await _rows(clean_tables, DiscoveryQuery))[0]
    assert query.next_eligible_at is not None
    assert "not configured" in (query.last_error or "")


async def test_a_disabled_query_is_skipped_without_a_call(
    clean_tables: Database,
) -> None:
    query_id = await _seed_query(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(sa.update(DiscoveryQuery).values(enabled=False))

    provider = FakeProvider(
        SourceProvider.BRAVE, WebDiscoveryKind.ROUTINE, outcome=_outcome("https://a.example/x")
    )
    await handle_web_discovery_search(
        _context(_container(clean_tables, provider), clean_tables, query_id)
    )
    assert provider.queries == []
    assert await _rows(clean_tables, ProviderCall) == []


# ---------------------------------------------------------------------------
# Cross-provider deduplication
# ---------------------------------------------------------------------------
async def test_the_same_page_from_both_providers_produces_one_event(
    clean_tables: Database,
) -> None:
    """Two providers finding one article is corroboration, not two events.

    Nothing downstream re-classifies it, nothing re-researches it, and the
    second sighting is recorded on the row that already exists.
    """
    routine_id = await _seed_query(clean_tables)
    async with clean_tables.transaction() as session:
        topic_id = (await session.execute(sa.select(DiscoveryQuery.topic_id))).scalars().first()
        semantic = DiscoveryQuery(
            topic_id=topic_id,
            query="who benefits from a transformer shortage",
            enabled=True,
            search_kind=WebDiscoveryKind.SEMANTIC,
        )
        session.add(semantic)
        await session.flush()
        semantic_id = semantic.id

    shared = "https://reuters.com/the-same-article"
    await handle_web_discovery_search(
        _context(
            _container(
                clean_tables,
                FakeProvider(
                    SourceProvider.BRAVE, WebDiscoveryKind.ROUTINE, outcome=_outcome(shared)
                ),
            ),
            clean_tables,
            routine_id,
        )
    )
    await handle_web_discovery_search(
        _context(
            _container(
                clean_tables,
                FakeProvider(
                    SourceProvider.EXA,
                    WebDiscoveryKind.SEMANTIC,
                    outcome=_outcome(shared, provider=SourceProvider.EXA),
                ),
            ),
            clean_tables,
            semantic_id,
        )
    )

    sources = await _rows(clean_tables, Source)
    events = await _rows(clean_tables, Event)
    assert len(sources) == 1
    assert len(events) == 1
    assert sources[0].provider is SourceProvider.BRAVE
    assert sources[0].discovered_by == ["BRAVE", "EXA"]
    # Both searches were still paid for: the provider returned a result either
    # way, and the deduplication happened after the money was spent.
    assert {row.provider for row in await _rows(clean_tables, ProviderCall)} == {"brave", "exa"}
