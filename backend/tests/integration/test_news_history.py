"""The historical cache: idempotency, bounds, as_of filtering and coverage.

Every test runs against the real migrations, because the guarantees being
checked -- a partial unique index, a forward-only watermark, a bounded run -- are
properties of the database, not of a mock.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.db.models.companies import Company
from stockbrain.db.models.news_history import (
    DATASET_CANDLES,
    DATASET_NEWS,
    RUN_STATUS_PARTIAL,
    NewsHistoryArticle,
    NewsHistoryBackfillRun,
    NewsHistoryCandle,
    NewsHistoryCoverage,
)
from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.db.session import Database
from stockbrain.enums import BarTimeframe, SourceProvider
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.intelligence.news_history import (
    GLOBAL_SCOPE,
    MATCH_EXACT,
    MATCH_LOOSER_FALLBACK,
    ArticleInput,
    BackfillLimits,
    CandleInput,
    NewsHistoryCache,
)
from stockbrain.market_data.base import Bar

pytestmark = pytest.mark.integration

UTC = dt.UTC
NOW = dt.datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def _clean_cache_tables(clean_tables: Database) -> AsyncIterator[Database]:
    """The shared truncate list predates this cache; clear its tables here.

    ``clean_tables`` truncates the tables it knows about with ``CASCADE``, which
    reaches the article table through its foreign keys but not the standalone
    candle, coverage or run tables.  Truncating all four makes the fixture's
    promise -- one test cannot see another's rows -- hold for this module too.
    """
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.text(
                "TRUNCATE news_history_backfill_runs, news_history_coverage, "
                "news_history_candles, news_history_articles RESTART IDENTITY CASCADE"
            )
        )
    yield clean_tables


async def _create_company(database: Database, symbol: str = "AAPL") -> uuid.UUID:
    async with database.transaction() as session:
        company = Company(name=f"{symbol} Inc.", primary_symbol=symbol)
        session.add(company)
        await session.flush()
        return company.id


async def _count(database: Database, model: type[Any]) -> int:
    async with database.session() as session:
        return int(
            (await session.execute(sa.select(sa.func.count()).select_from(model))).scalar_one()
        )


def _article(
    *,
    provider_item_id: str,
    headline: str,
    observed_at: dt.datetime,
    company_id: uuid.UUID | None = None,
    event_type: str | None = None,
    symbols: tuple[str, ...] = ("AAPL",),
) -> ArticleInput:
    return ArticleInput(
        provider=SourceProvider.ALPACA,
        provider_item_id=provider_item_id,
        headline=headline,
        symbols=symbols,
        published_at=observed_at,
        observed_at=observed_at,
        feed="alpaca:backfill",
        company_id=company_id,
        event_type=event_type,
    )


class _FakeNewsClient:
    name = "fake_news"

    def __init__(self, documents: Sequence[RawSourceDocument]) -> None:
        self._documents = list(documents)
        self.calls: list[tuple[dt.datetime, dt.datetime, int | None]] = []

    async def backfill(
        self,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
        symbols: Sequence[str] | None = None,
        max_pages: int | None = None,
    ) -> Sequence[RawSourceDocument]:
        self.calls.append((start, end, limit))
        return list(self._documents)


class _FakeBarsProvider:
    name = "fake_bars"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dt.datetime, dt.datetime]] = []
        self.last_bars_requests = 1
        self.last_bars_truncated = False

    async def bars(
        self,
        symbol: str,
        timeframe: BarTimeframe,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
        max_pages: int | None = None,
    ) -> Sequence[Bar]:
        self.calls.append((symbol, start, end))
        return [
            Bar(
                symbol=symbol,
                timestamp=start,
                open=Decimal("10"),
                high=Decimal("10.5"),
                low=Decimal("9.5"),
                close=Decimal("10.25"),
                volume=100,
                feed="iex",
            )
        ]

    @property
    def call_count(self) -> int:
        return len(self.calls)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
async def test_recording_the_same_article_twice_writes_one_row(clean_tables: Database) -> None:
    company_id = await _create_company(clean_tables)
    cache = NewsHistoryCache(clean_tables)
    article = _article(
        provider_item_id="alpaca-1",
        headline="Apple beats estimates",
        observed_at=NOW,
        company_id=company_id,
        event_type="EARNINGS",
    )

    first = await cache.record_articles([article])
    second = await cache.record_articles([article])

    assert first.written == 1
    assert first.duplicates == 0
    assert second.written == 0
    assert second.duplicates == 1
    assert await _count(clean_tables, NewsHistoryArticle) == 1
    # The canonical graph is written once too, not once per attempt.
    assert await _count(clean_tables, Event) == 1
    assert await _count(clean_tables, Source) == 1
    assert await _count(clean_tables, EventSourceLink) == 1


async def test_candles_are_idempotent_and_as_of_filtered(clean_tables: Database) -> None:
    cache = NewsHistoryCache(clean_tables)
    bar = CandleInput(
        symbol="AAPL",
        bar_start=NOW,
        open=Decimal("10"),
        high=Decimal("10.5"),
        low=Decimal("9.5"),
        close=Decimal("10.25"),
        volume=100,
        feed="iex",
        provider="alpaca",
        observed_at=NOW + dt.timedelta(minutes=1),
    )

    first = await cache.record_candles([bar])
    second = await cache.record_candles([bar])

    assert first.written == 1
    assert second.written == 0
    assert second.duplicates == 1
    assert await _count(clean_tables, NewsHistoryCandle) == 1

    # A bar that has not closed yet is invisible to a point-in-time query.
    assert await cache.query_candles(
        symbol="AAPL", start=NOW - dt.timedelta(hours=1), end=NOW + dt.timedelta(hours=1)
    )
    before_close = await cache.query_candles(
        symbol="AAPL",
        start=NOW - dt.timedelta(hours=1),
        end=NOW + dt.timedelta(hours=1),
        as_of=NOW,
    )
    after_close = await cache.query_candles(
        symbol="AAPL",
        start=NOW - dt.timedelta(hours=1),
        end=NOW + dt.timedelta(hours=1),
        as_of=NOW + dt.timedelta(minutes=2),
    )
    assert before_close == []
    assert len(after_close) == 1


async def test_candle_without_observed_at_is_visible_only_after_bar_close(
    clean_tables: Database,
) -> None:
    cache = NewsHistoryCache(clean_tables)
    await cache.record_candles(
        [
            CandleInput(
                symbol="AAPL",
                bar_start=NOW,
                open=Decimal("10"),
                high=Decimal("10"),
                low=Decimal("10"),
                close=Decimal("10"),
                feed="iex",
            )
        ]
    )
    before = await cache.query_candles(
        symbol="AAPL",
        start=NOW,
        end=NOW + dt.timedelta(minutes=1),
        as_of=NOW + dt.timedelta(seconds=30),
    )
    after = await cache.query_candles(
        symbol="AAPL",
        start=NOW,
        end=NOW + dt.timedelta(minutes=1),
        as_of=NOW + dt.timedelta(minutes=1),
    )
    assert before == []
    assert len(after) == 1


# ---------------------------------------------------------------------------
# Duplicate grouping onto a canonical event
# ---------------------------------------------------------------------------
async def test_syndicated_articles_group_onto_one_event(clean_tables: Database) -> None:
    company_id = await _create_company(clean_tables)
    cache = NewsHistoryCache(clean_tables)

    await cache.record_articles(
        [
            _article(
                provider_item_id="alpaca-1",
                headline="Apple beats estimates",
                observed_at=NOW,
                company_id=company_id,
                event_type="EARNINGS",
            ),
            _article(
                provider_item_id="alpaca-2",
                headline="Apple beats estimates",
                observed_at=NOW + dt.timedelta(minutes=5),
                company_id=company_id,
                event_type="EARNINGS",
            ),
        ]
    )

    result = await cache.query_articles(
        as_of=NOW + dt.timedelta(hours=1), company_id=company_id, event_type="EARNINGS"
    )

    assert result.match_mode == MATCH_EXACT
    assert result.event_count == 1
    assert result.article_count == 2
    group = result.groups[0]
    assert group.canonical is True
    assert group.event_id is not None
    assert group.duplicate_count == 1
    assert await _count(clean_tables, Event) == 1
    assert await _count(clean_tables, EventSourceLink) == 2


async def test_symbol_filter_groups_only_matching_articles(clean_tables: Database) -> None:
    company_id = await _create_company(clean_tables)
    cache = NewsHistoryCache(clean_tables)
    await cache.record_articles(
        [
            _article(
                provider_item_id="a-aapl",
                headline="Apple news",
                observed_at=NOW,
                company_id=company_id,
                symbols=("AAPL",),
            ),
            _article(
                provider_item_id="a-msft",
                headline="Microsoft news",
                observed_at=NOW,
                company_id=company_id,
                symbols=("MSFT",),
            ),
        ]
    )

    result = await cache.query_articles(as_of=NOW + dt.timedelta(minutes=1), symbols=["AAPL"])

    assert result.article_count == 1
    assert result.articles[0].symbols == ["AAPL"]


# ---------------------------------------------------------------------------
# as_of filtering
# ---------------------------------------------------------------------------
async def test_as_of_hides_articles_that_were_not_yet_observable(
    clean_tables: Database,
) -> None:
    company_id = await _create_company(clean_tables)
    cache = NewsHistoryCache(clean_tables)
    await cache.record_articles(
        [
            _article(
                provider_item_id="old",
                headline="Apple morning news",
                observed_at=NOW - dt.timedelta(hours=2),
                company_id=company_id,
            ),
            _article(
                provider_item_id="new",
                headline="Apple afternoon news",
                observed_at=NOW,
                company_id=company_id,
            ),
        ]
    )

    before = await cache.query_articles(as_of=NOW - dt.timedelta(hours=1), company_id=company_id)
    at = await cache.query_articles(as_of=NOW, company_id=company_id)

    assert before.article_count == 1
    assert before.articles[0].provider_item_id == "old"
    assert at.article_count == 2


# ---------------------------------------------------------------------------
# Exact match and the labelled looser fallback
# ---------------------------------------------------------------------------
async def test_exact_match_then_labelled_looser_fallback(clean_tables: Database) -> None:
    company_id = await _create_company(clean_tables)
    cache = NewsHistoryCache(clean_tables)
    await cache.record_articles(
        [
            _article(
                provider_item_id="earnings",
                headline="Apple reports earnings",
                observed_at=NOW,
                company_id=company_id,
                event_type="EARNINGS",
            )
        ]
    )

    exact = await cache.query_articles(
        as_of=NOW + dt.timedelta(hours=1), company_id=company_id, event_type="EARNINGS"
    )
    miss = await cache.query_articles(
        as_of=NOW + dt.timedelta(hours=1), company_id=company_id, event_type="ANALYST_RATING"
    )
    fallback = await cache.query_articles(
        as_of=NOW + dt.timedelta(hours=1),
        company_id=company_id,
        event_type="ANALYST_RATING",
        allow_loose_fallback=True,
    )

    assert exact.match_mode == MATCH_EXACT
    assert exact.article_count == 1
    assert miss.match_mode == MATCH_EXACT
    assert miss.is_empty
    assert fallback.match_mode == MATCH_LOOSER_FALLBACK
    assert fallback.fallback_reason is not None
    assert fallback.article_count == 1


# ---------------------------------------------------------------------------
# Bounds and partial coverage
# ---------------------------------------------------------------------------
async def test_backfill_news_clamps_window_and_records_partial_coverage(
    clean_tables: Database,
) -> None:
    cache = NewsHistoryCache(clean_tables)
    document = RawSourceDocument(
        provider=SourceProvider.ALPACA,
        provider_item_id="a1",
        headline="Apple beats estimates",
        symbols=["AAPL"],
        published_at=NOW - dt.timedelta(hours=1),
    )
    client = _FakeNewsClient([document])
    start = NOW - dt.timedelta(days=10)

    report = await cache.backfill_news(
        start=start,
        end=NOW,
        limits=BackfillLimits(max_days=2, max_articles=10, max_requests=5),
        client=client,
        now=NOW,
    )

    assert report.bounded is True
    assert report.partial is True
    assert report.stop_reason is not None and "max_days" in report.stop_reason
    assert report.effective_start == NOW - dt.timedelta(days=2)
    assert report.articles_fetched == 1
    # The request cap is enforced through the client's document budget.
    assert client.calls[0][0] == NOW - dt.timedelta(days=2)
    assert client.calls[0][2] == 10

    coverage = await cache.coverage(dataset=DATASET_NEWS, scope_key=GLOBAL_SCOPE)
    assert coverage is not None
    assert coverage.is_partial is True
    assert coverage.detail is not None and "max_days" in coverage.detail
    assert coverage.article_count == 1

    run = await cache.latest_run(dataset=DATASET_NEWS, scope_key=GLOBAL_SCOPE)
    assert run is not None
    assert run.status == RUN_STATUS_PARTIAL
    assert run.bounded is True


async def test_backfill_news_stops_at_the_article_cap(clean_tables: Database) -> None:
    cache = NewsHistoryCache(clean_tables)
    documents = [
        RawSourceDocument(
            provider=SourceProvider.ALPACA,
            provider_item_id=f"a{index}",
            headline=f"Headline {index}",
            published_at=NOW,
        )
        for index in range(3)
    ]
    client = _FakeNewsClient(documents)

    report = await cache.backfill_news(
        start=NOW - dt.timedelta(hours=1),
        end=NOW,
        limits=BackfillLimits(max_days=1, max_articles=3, max_requests=1),
        client=client,
        now=NOW,
    )

    assert report.articles_fetched == 3
    assert report.bounded is True
    assert report.stop_reason is not None and "max_articles" in report.stop_reason
    # A capped fetch cannot exceed the budget it asked the client for.
    assert client.calls[0][2] == 3


async def test_backfill_candles_stops_at_the_request_cap(clean_tables: Database) -> None:
    cache = NewsHistoryCache(clean_tables)
    provider = _FakeBarsProvider()

    report = await cache.backfill_candles(
        symbol="AAPL",
        start=NOW - dt.timedelta(days=10),
        end=NOW,
        limits=BackfillLimits(max_days=10, max_bar_pages=3, max_requests=3),
        provider=provider,
        now=NOW,
    )

    assert provider.call_count == 3
    assert report.requests_made == 3
    assert report.pages_fetched == 3
    assert report.bounded is True
    assert report.partial is True
    assert report.stop_reason in {"max_bar_pages", "max_requests"}
    assert await _count(clean_tables, NewsHistoryCandle) == 3

    coverage = await cache.coverage(dataset=DATASET_CANDLES, scope_key="AAPL")
    assert coverage is not None
    assert coverage.is_partial is True
    assert coverage.request_count == 3
    assert coverage.bar_count == 3

    run = await cache.latest_run(dataset=DATASET_CANDLES, scope_key="AAPL")
    assert run is not None
    assert run.status == RUN_STATUS_PARTIAL


async def test_candle_provider_page_truncation_is_recorded_as_partial(
    clean_tables: Database,
) -> None:
    provider = _FakeBarsProvider()
    provider.last_bars_requests = 1
    provider.last_bars_truncated = True
    report = await NewsHistoryCache(clean_tables).backfill_candles(
        symbol="AAPL",
        start=NOW - dt.timedelta(days=1),
        end=NOW,
        provider=provider,
        now=NOW,
    )
    assert provider.call_count == 1
    assert report.requests_made == 1
    assert report.partial is True
    assert report.stop_reason == "provider_page_cap"


async def test_disjoint_completed_backfills_do_not_claim_contiguous_coverage(
    clean_tables: Database,
) -> None:
    cache = NewsHistoryCache(clean_tables)
    provider = _FakeBarsProvider()
    limits = BackfillLimits(max_days=2, max_bar_pages=20, max_requests=20)
    await cache.backfill_candles(
        symbol="AAPL",
        start=NOW - dt.timedelta(days=10),
        end=NOW - dt.timedelta(days=9),
        limits=limits,
        provider=provider,
        now=NOW,
    )
    await cache.backfill_candles(
        symbol="AAPL",
        start=NOW - dt.timedelta(days=1),
        end=NOW,
        limits=limits,
        provider=provider,
        now=NOW,
    )
    coverage = await cache.coverage(dataset=DATASET_CANDLES, scope_key="AAPL")
    assert coverage is not None
    assert coverage.is_partial is True
    assert coverage.detail == "disjoint_coverage_window"


async def test_an_older_write_does_not_regress_the_watermark(
    clean_tables: Database,
) -> None:
    cache = NewsHistoryCache(clean_tables)
    first = _article(
        provider_item_id="newer",
        headline="Apple newer news",
        observed_at=NOW,
    )
    older = _article(
        provider_item_id="older",
        headline="Apple older news",
        observed_at=NOW - dt.timedelta(hours=3),
    )
    await cache.record_articles([first], scope_key="AAPL", update_coverage=True)
    await cache.record_articles([older], scope_key="AAPL", update_coverage=True)

    coverage = await cache.coverage(dataset=DATASET_NEWS, scope_key="AAPL")
    assert coverage is not None
    assert coverage.watermark_at == NOW


async def test_latest_run_is_scoped_to_its_dataset_and_key(clean_tables: Database) -> None:
    cache = NewsHistoryCache(clean_tables)
    client = _FakeNewsClient([])

    await cache.backfill_news(
        start=NOW - dt.timedelta(hours=1),
        end=NOW,
        symbols=["AAPL"],
        limits=BackfillLimits(max_days=1, max_articles=5, max_requests=5),
        client=client,
        now=NOW,
    )

    assert await cache.latest_run(dataset=DATASET_NEWS, scope_key="AAPL") is not None
    assert await cache.latest_run(dataset=DATASET_NEWS, scope_key=GLOBAL_SCOPE) is None
    assert await _count(clean_tables, NewsHistoryBackfillRun) == 1
    assert await _count(clean_tables, NewsHistoryCoverage) == 1


async def test_backfill_requires_an_explicit_provider(clean_tables: Database) -> None:
    """No provider means no implicit network access -- the call refuses."""
    cache = NewsHistoryCache(clean_tables)

    with pytest.raises(ValueError, match="explicit provider"):
        await cache.backfill_candles(
            symbol="AAPL",
            start=NOW - dt.timedelta(hours=1),
            end=NOW,
            limits=BackfillLimits(max_days=1),
            now=NOW,
        )


async def test_a_read_never_starts_a_backfill(clean_tables: Database) -> None:
    """The query surface holds no provider, so a research read cannot fetch.

    The backfill that seeded the cache is the only run; reading afterwards
    creates no new run and leaves the bounded coverage exactly as the run wrote
    it.
    """
    cache = NewsHistoryCache(clean_tables)
    document = RawSourceDocument(
        provider=SourceProvider.ALPACA,
        provider_item_id="a1",
        headline="Apple beats estimates",
        published_at=NOW - dt.timedelta(hours=1),
    )
    client = _FakeNewsClient([document])
    await cache.backfill_news(
        start=NOW - dt.timedelta(hours=2),
        end=NOW,
        limits=BackfillLimits(max_days=1, max_articles=5, max_requests=5),
        client=client,
        now=NOW,
    )
    runs_before = await _count(clean_tables, NewsHistoryBackfillRun)

    result = await cache.query_articles(as_of=NOW + dt.timedelta(hours=1))

    assert result.article_count == 1
    assert await _count(clean_tables, NewsHistoryBackfillRun) == runs_before == 1
