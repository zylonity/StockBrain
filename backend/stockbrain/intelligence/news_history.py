"""Cache-only historical news and candle access for research and grading.

Two rules shape this module.

**Reads never call a provider.**  :meth:`NewsHistoryCache.query_articles`,
:meth:`NewsHistoryCache.query_candles` and the coverage lookups only touch the
database.  A research run cannot accidentally trigger a network fetch by asking
a question.

**Backfills are explicit and bounded.**  :meth:`NewsHistoryCache.backfill_news`
and :meth:`NewsHistoryCache.backfill_candles` are *only* called on demand (an
operator action or a dedicated scheduled job).  They reuse
:meth:`~stockbrain.ingestion.alpaca_news.AlpacaNewsClient.backfill` and
:meth:`~stockbrain.market_data.base.MarketDataProvider.bars`, take a
:class:`BackfillLimits`, and stop at the first cap they hit.  A run that stops
early records ``PARTIAL`` coverage with the cap that bit -- the incompleteness is
a fact in the database, not an inference from missing rows.

Point-in-time correctness is the reason ``observed_at`` exists as a column rather
than being derived. An article published on Monday and backfilled on Friday is
publicly observable on Tuesday, but StockBrain did not receive it on Monday.
``ingested_at`` records the Friday cache import; actual live receipt is unknown.
Research packets require both ``observed_at`` and ``ingested_at <= as_of``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, cast, runtime_checkable

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import Company
from stockbrain.db.models.news_history import (
    DATASET_CANDLES,
    DATASET_NEWS,
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCEEDED,
    NewsHistoryArticle,
    NewsHistoryBackfillRun,
    NewsHistoryCandle,
    NewsHistoryCoverage,
)
from stockbrain.db.models.sources import Event
from stockbrain.db.session import Database
from stockbrain.enums import (
    BarTimeframe,
    EventStatus,
    SourceCategory,
    SourceProvider,
)
from stockbrain.ingestion.alpaca_news import ALPACA_NEWS_MAX_LIMIT
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.ingestion.dedupe import DEFAULT_EVENT_MATCH_WINDOW, title_hash
from stockbrain.ingestion.normalizer import content_hash
from stockbrain.logging import get_logger

__all__ = [
    "GLOBAL_SCOPE",
    "MATCH_EXACT",
    "MATCH_LOOSER_FALLBACK",
    "ArticleInput",
    "ArticleQueryResult",
    "BackfillLimits",
    "BackfillReport",
    "BarsProvider",
    "CandleInput",
    "NewsBackfillClient",
    "NewsEventGroup",
    "NewsHistoryCache",
    "RecordResult",
    "recorded_article_to_input",
]

log = get_logger(__name__)

#: Coverage scope for a backfill that is not narrowed to one symbol/company.
GLOBAL_SCOPE = "GLOBAL"

#: ``ArticleQueryResult.match_mode`` values.  The looser mode is always labelled
#: so a caller can never mistake a fallback match for an exact one.
MATCH_EXACT = "exact"
MATCH_LOOSER_FALLBACK = "looser_fallback"

_TIMEFRAME_DELTAS: dict[BarTimeframe, dt.timedelta] = {
    BarTimeframe.MIN_1: dt.timedelta(minutes=1),
    BarTimeframe.MIN_5: dt.timedelta(minutes=5),
    BarTimeframe.MIN_15: dt.timedelta(minutes=15),
    BarTimeframe.HOUR_1: dt.timedelta(hours=1),
    BarTimeframe.DAY_1: dt.timedelta(days=1),
}


def _as_utc(value: dt.datetime) -> dt.datetime:
    """Reject naive datetimes; normalise aware ones to UTC.

    Naive input is refused rather than assumed, because the whole module exists
    to make a time boundary trustworthy and a guessed timezone breaks exactly
    that.
    """
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(dt.UTC)


def _clamp_positive(value: int, label: str) -> int:
    if value < 1:
        raise ValueError(f"{label} must be >= 1, got {value}")
    return value


#: Rows per candle INSERT; 16 columns each stays well under PostgreSQL's limit.
CANDLE_INSERT_BATCH = 1000


@dataclass(slots=True, frozen=True)
class BackfillLimits:
    """The caps one explicit backfill run may consume.

    All four are enforced: the window is truncated to ``max_days``, news is
    fetched with a document budget of ``max_articles``, and the candle loop makes
    at most ``min(max_bar_pages, max_requests)`` provider calls.  Hitting any of
    them ends the run early and marks coverage partial.
    """

    max_days: int = 7
    max_articles: int = 500
    max_bar_pages: int = 20
    max_requests: int = 20

    def __post_init__(self) -> None:
        _clamp_positive(self.max_days, "max_days")
        _clamp_positive(self.max_articles, "max_articles")
        _clamp_positive(self.max_bar_pages, "max_bar_pages")
        _clamp_positive(self.max_requests, "max_requests")

    def as_json(self) -> dict[str, int]:
        return {
            "max_days": self.max_days,
            "max_articles": self.max_articles,
            "max_bar_pages": self.max_bar_pages,
            "max_requests": self.max_requests,
        }

    @property
    def effective_request_cap(self) -> int:
        return min(self.max_bar_pages, self.max_requests)


@dataclass(slots=True, frozen=True)
class ArticleInput:
    """One article to cache, already normalised out of provider JSON."""

    provider: SourceProvider
    provider_item_id: str | None
    headline: str | None = None
    body: str | None = None
    symbols: tuple[str, ...] = ()
    published_at: dt.datetime | None = None
    observed_at: dt.datetime | None = None
    feed: str = "unknown"
    url: str | None = None
    source_name: str | None = None
    source_category: SourceCategory = SourceCategory.UNKNOWN
    company_id: uuid.UUID | None = None
    event_type: str | None = None
    content_hash: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved_content_hash(self) -> str:
        return self.content_hash or content_hash(self.headline, self.body)


@dataclass(slots=True, frozen=True)
class CandleInput:
    """One OHLCV bar to cache."""

    symbol: str
    bar_start: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    trade_count: int | None = None
    vwap: Decimal | None = None
    timeframe: str = BarTimeframe.MIN_1.value
    provider: str = "unknown"
    feed: str = "unknown"
    observed_at: dt.datetime | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class RecordResult:
    written: int
    duplicates: int


@dataclass(slots=True, frozen=True)
class BackfillReport:
    dataset: str
    scope_key: str
    status: str
    effective_start: dt.datetime | None
    effective_end: dt.datetime | None
    articles_fetched: int
    bars_fetched: int
    pages_fetched: int
    requests_made: int
    bounded: bool
    partial: bool
    stop_reason: str | None
    watermark_at: dt.datetime | None

    @property
    def wrote_rows(self) -> int:
        return self.articles_fetched + self.bars_fetched


@dataclass(slots=True, frozen=True)
class NewsEventGroup:
    """Articles that share one canonical event (or one lone article)."""

    event_id: uuid.UUID | None
    articles: tuple[NewsHistoryArticle, ...]
    canonical: bool

    @property
    def duplicate_count(self) -> int:
        return max(0, len(self.articles) - 1)


@dataclass(slots=True, frozen=True)
class ArticleQueryResult:
    as_of: dt.datetime
    match_mode: str
    fallback_reason: str | None
    groups: tuple[NewsEventGroup, ...]

    @property
    def articles(self) -> tuple[NewsHistoryArticle, ...]:
        return tuple(article for group in self.groups for article in group.articles)

    @property
    def article_count(self) -> int:
        return len(self.articles)

    @property
    def event_count(self) -> int:
        return len(self.groups)

    @property
    def is_empty(self) -> bool:
        return not self.groups


@dataclass(slots=True, frozen=True)
class _WriteOutcome:
    written: int
    duplicates: int
    watermark_at: dt.datetime | None


@runtime_checkable
class NewsBackfillClient(Protocol):
    """The slice of :class:`AlpacaNewsClient` this module consumes."""

    name: str

    async def backfill(
        self,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
        symbols: Sequence[str] | None = None,
        max_pages: int | None = None,
    ) -> Sequence[RawSourceDocument]: ...


@runtime_checkable
class BarsProvider(Protocol):
    """The slice of :class:`MarketDataProvider` this module consumes."""

    name: str

    async def bars(
        self,
        symbol: str,
        timeframe: BarTimeframe,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
        max_pages: int | None = None,
    ) -> Sequence[Any]: ...


def recorded_article_to_input(document: RawSourceDocument, *, feed: str) -> ArticleInput:
    """Adapt an Alpaca backfill document to :class:`ArticleInput`.

    Kept public so a caller assembling its own backfill loop does not have to
    re-derive the observed-at rule.
    """
    return ArticleInput(
        provider=document.provider,
        provider_item_id=document.provider_item_id,
        headline=document.headline,
        body=document.body,
        symbols=tuple(symbol.strip().upper() for symbol in document.symbols if symbol.strip()),
        published_at=document.published_at,
        observed_at=document.published_at,
        feed=feed,
        url=document.url,
        source_name=document.source_name,
        source_category=document.source_category,
        raw_payload=dict(document.raw_payload),
    )


class NewsHistoryCache:
    """Database-backed historical cache.  Reads are cache-only.

    The class owns no provider clients.  A caller passes one in to the explicit
    backfill methods, which is what keeps "backfill" a deliberate act.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------
    # Cache writes (no network)
    # ------------------------------------------------------------------
    async def record_articles(
        self,
        articles: Sequence[ArticleInput],
        *,
        ingested_at: dt.datetime | None = None,
        scope_key: str = GLOBAL_SCOPE,
        update_coverage: bool = False,
    ) -> RecordResult:
        """Persist articles, grouping duplicates onto canonical events.

        Idempotent on ``(provider, provider_item_id)``; rows without one are
        deduplicated on ``(provider, content_hash)``.  A duplicate is counted,
        never rewritten -- the first observation's ``ingested_at`` is the truth.
        """
        stamp = _as_utc(ingested_at or utcnow())
        async with self._database.transaction() as session:
            outcome = await self._write_articles(session, articles, ingested_at=stamp)
            if update_coverage:
                await self._touch_coverage(
                    session,
                    dataset=DATASET_NEWS,
                    scope_key=scope_key,
                    watermark_at=outcome.watermark_at,
                    article_delta=outcome.written,
                    request_delta=0,
                    attempted_at=stamp,
                    is_partial=None,
                    detail=None,
                )
        return RecordResult(written=outcome.written, duplicates=outcome.duplicates)

    async def record_candles(
        self,
        candles: Sequence[CandleInput],
        *,
        ingested_at: dt.datetime | None = None,
        scope_key: str | None = None,
        update_coverage: bool = False,
    ) -> RecordResult:
        """Persist bars idempotently on ``(symbol, timeframe, bar_start, feed)``."""
        stamp = _as_utc(ingested_at or utcnow())
        async with self._database.transaction() as session:
            outcome = await self._write_candles(session, candles, ingested_at=stamp)
            if update_coverage and candles:
                await self._touch_coverage(
                    session,
                    dataset=DATASET_CANDLES,
                    scope_key=scope_key or candles[0].symbol.strip().upper(),
                    watermark_at=outcome.watermark_at,
                    bar_delta=outcome.written,
                    request_delta=0,
                    attempted_at=stamp,
                    is_partial=None,
                    detail=None,
                )
        return RecordResult(written=outcome.written, duplicates=outcome.duplicates)

    # ------------------------------------------------------------------
    # Explicit, bounded backfills
    # ------------------------------------------------------------------
    async def backfill_news(
        self,
        *,
        start: dt.datetime,
        end: dt.datetime,
        symbols: Sequence[str] | None = None,
        limits: BackfillLimits | None = None,
        client: NewsBackfillClient | None = None,
        now: dt.datetime | None = None,
    ) -> BackfillReport:
        """Fetch and persist historical news for ``[start, end]``.

        On-demand only.  The window is truncated to ``limits.max_days`` and the
        fetch is given an article budget no larger than
        ``min(max_articles, max_requests * 50)`` because Alpaca pages 50 at a
        time -- which is how the request cap is enforced through the client.
        """
        caps = limits or BackfillLimits()
        started = _as_utc(now or utcnow())
        window_start, window_end = self._bounded_window(start, end, caps)
        truncated_days = window_start > _as_utc(start)
        scope_key = self._news_scope(symbols)
        run_id = await self._start_run(
            dataset=DATASET_NEWS,
            scope_key=scope_key,
            start=_as_utc(start),
            end=_as_utc(end),
            limits=caps,
            started_at=started,
        )

        article_budget = min(caps.max_articles, caps.max_requests * ALPACA_NEWS_MAX_LIMIT)
        if client is None:
            await self._fail_run(run_id, "no news backfill client supplied", started)
            raise ValueError("backfill_news requires an explicit client")

        try:
            documents = list(
                await client.backfill(
                    window_start,
                    window_end,
                    limit=article_budget,
                    symbols=symbols,
                    max_pages=caps.max_requests,
                )
            )
        except Exception as exc:
            await self._fail_run(run_id, f"{type(exc).__name__}: {exc}", started)
            raise

        bounded_articles = len(documents) >= article_budget
        inputs = [
            recorded_article_to_input(document, feed="alpaca:backfill") for document in documents
        ]
        pages = getattr(client, "last_backfill_requests", None)
        if pages is None:
            pages = max(1, -(-len(documents) // ALPACA_NEWS_MAX_LIMIT)) if documents else 1

        stop_reason: str | None = None
        if truncated_days:
            stop_reason = "max_days"
        if bounded_articles:
            stop_reason = "max_articles" if stop_reason is None else f"{stop_reason}+max_articles"
        if getattr(client, "last_backfill_truncated", False):
            stop_reason = "max_requests" if stop_reason is None else f"{stop_reason}+max_requests"
        partial = stop_reason is not None

        written: _WriteOutcome | None = None
        try:
            async with self._database.transaction() as session:
                if inputs:
                    written = await self._write_articles(session, inputs, ingested_at=started)
                watermark = written.watermark_at if written else None
                await self._touch_coverage(
                    session,
                    dataset=DATASET_NEWS,
                    scope_key=scope_key,
                    watermark_at=watermark,
                    article_delta=written.written if written else 0,
                    request_delta=pages,
                    attempted_at=started,
                    is_partial=partial,
                    detail=stop_reason,
                    window_start=window_start if inputs else None,
                    window_end=window_end if inputs else None,
                )
                await self._finish_run(
                    session,
                    run_id,
                    status=RUN_STATUS_PARTIAL if partial else RUN_STATUS_SUCCEEDED,
                    articles_fetched=written.written if written else 0,
                    bars_fetched=0,
                    pages_fetched=pages,
                    requests_made=pages,
                    bounded=partial,
                    stop_reason=stop_reason,
                    watermark_at=watermark,
                    finished_at=started,
                )
        except Exception as exc:
            await self._fail_run(run_id, f"{type(exc).__name__}: {exc}", started)
            raise

        return BackfillReport(
            dataset=DATASET_NEWS,
            scope_key=scope_key,
            status=RUN_STATUS_PARTIAL if partial else RUN_STATUS_SUCCEEDED,
            effective_start=window_start,
            effective_end=window_end,
            articles_fetched=written.written if written else 0,
            bars_fetched=0,
            pages_fetched=pages,
            requests_made=pages,
            bounded=partial,
            partial=partial,
            stop_reason=stop_reason,
            watermark_at=watermark,
        )

    async def backfill_candles(
        self,
        *,
        symbol: str,
        start: dt.datetime,
        end: dt.datetime,
        timeframe: BarTimeframe = BarTimeframe.MIN_1,
        limits: BackfillLimits | None = None,
        provider: BarsProvider | None = None,
        now: dt.datetime | None = None,
    ) -> BackfillReport:
        """Fetch and persist historical bars for one symbol.

        On-demand only. Each daily chunk permits one provider HTTP page of at
        most 1,000 bars. The run stops at the explicit request budget.
        """
        caps = limits or BackfillLimits()
        started = _as_utc(now or utcnow())
        ticker = symbol.strip().upper()
        if not ticker:
            raise ValueError("symbol must be non-empty")
        window_start, window_end = self._bounded_window(start, end, caps)
        truncated_days = window_start > _as_utc(start)
        run_id = await self._start_run(
            dataset=DATASET_CANDLES,
            scope_key=ticker,
            start=_as_utc(start),
            end=_as_utc(end),
            limits=caps,
            started_at=started,
        )
        if provider is None:
            await self._fail_run(run_id, "no bars provider supplied", started)
            raise ValueError("backfill_candles requires an explicit provider")

        request_cap = caps.effective_request_cap
        collected: list[CandleInput] = []
        requests_made = 0
        hit_request_cap = False
        hit_page_cap = False
        try:
            cursor = window_start
            step = dt.timedelta(days=1)
            while cursor < window_end:
                if requests_made >= request_cap:
                    hit_request_cap = True
                    break
                chunk_end = min(cursor + step, window_end)
                bars = await provider.bars(
                    ticker, timeframe, cursor, chunk_end, limit=1000, max_pages=1
                )
                requests_made += int(getattr(provider, "last_bars_requests", 1))
                if getattr(provider, "last_bars_truncated", False):
                    hit_page_cap = True
                for bar in bars:
                    collected.append(
                        CandleInput(
                            symbol=ticker,
                            bar_start=_as_utc(bar.timestamp),
                            open=bar.open,
                            high=bar.high,
                            low=bar.low,
                            close=bar.close,
                            volume=int(bar.volume),
                            trade_count=bar.trade_count,
                            vwap=bar.vwap,
                            timeframe=timeframe.value,
                            provider=provider.name,
                            feed=bar.feed or "unknown",
                            observed_at=_as_utc(bar.timestamp) + _TIMEFRAME_DELTAS[timeframe],
                        )
                    )
                cursor = chunk_end
                if hit_page_cap:
                    break
        except Exception as exc:
            await self._fail_run(run_id, f"{type(exc).__name__}: {exc}", started)
            raise

        stop_reason: str | None = None
        if truncated_days:
            stop_reason = "max_days"
        if hit_request_cap:
            label = "max_bar_pages" if caps.max_bar_pages <= caps.max_requests else "max_requests"
            stop_reason = label if stop_reason is None else f"{stop_reason}+{label}"
        if hit_page_cap:
            stop_reason = (
                "provider_page_cap" if stop_reason is None else f"{stop_reason}+provider_page_cap"
            )
        partial = stop_reason is not None

        written: _WriteOutcome | None = None
        try:
            async with self._database.transaction() as session:
                if collected:
                    written = await self._write_candles(session, collected, ingested_at=started)
                watermark = written.watermark_at if written else None
                await self._touch_coverage(
                    session,
                    dataset=DATASET_CANDLES,
                    scope_key=ticker,
                    watermark_at=watermark,
                    bar_delta=written.written if written else 0,
                    request_delta=requests_made,
                    attempted_at=started,
                    is_partial=partial,
                    detail=stop_reason,
                    window_start=window_start if collected else None,
                    window_end=window_end if collected else None,
                )
                await self._finish_run(
                    session,
                    run_id,
                    status=RUN_STATUS_PARTIAL if partial else RUN_STATUS_SUCCEEDED,
                    articles_fetched=0,
                    bars_fetched=written.written if written else 0,
                    pages_fetched=requests_made,
                    requests_made=requests_made,
                    bounded=partial,
                    stop_reason=stop_reason,
                    watermark_at=watermark,
                    finished_at=started,
                )
        except Exception as exc:
            # A failed write must leave an auditable FAILED run, not RUNNING.
            await self._fail_run(run_id, f"{type(exc).__name__}: {exc}", started)
            raise

        return BackfillReport(
            dataset=DATASET_CANDLES,
            scope_key=ticker,
            status=RUN_STATUS_PARTIAL if partial else RUN_STATUS_SUCCEEDED,
            effective_start=window_start,
            effective_end=window_end,
            articles_fetched=0,
            bars_fetched=written.written if written else 0,
            pages_fetched=requests_made,
            requests_made=requests_made,
            bounded=partial,
            partial=partial,
            stop_reason=stop_reason,
            watermark_at=watermark,
        )

    # ------------------------------------------------------------------
    # Cache reads
    # ------------------------------------------------------------------
    async def query_articles(
        self,
        *,
        as_of: dt.datetime,
        company_id: uuid.UUID | None = None,
        event_type: str | None = None,
        symbols: Sequence[str] | None = None,
        allow_loose_fallback: bool = False,
        limit: int = 500,
    ) -> ArticleQueryResult:
        """Articles observable at ``as_of``, grouped by canonical event.

        The exact filter is ``company_id`` AND ``event_type`` (and, when given,
        symbol overlap).  When both company and event type are supplied and the
        exact query is empty, ``allow_loose_fallback`` permits one clearly
        labelled retry that drops ``event_type``.  The fallback is never silent:
        the result carries ``MATCH_LOOSER_FALLBACK`` and the reason.
        """
        moment = _as_utc(as_of)
        if limit < 1:
            raise ValueError("limit must be >= 1")
        rows = await self._select_articles(
            as_of=moment,
            company_id=company_id,
            event_type=event_type,
            symbols=symbols,
            limit=limit,
        )
        if not rows and allow_loose_fallback and company_id is not None and event_type is not None:
            fallback_rows = await self._select_articles(
                as_of=moment,
                company_id=company_id,
                event_type=None,
                symbols=symbols,
                limit=limit,
            )
            if fallback_rows:
                return ArticleQueryResult(
                    as_of=moment,
                    match_mode=MATCH_LOOSER_FALLBACK,
                    fallback_reason=(
                        "no articles matched company_id and event_type exactly; "
                        f"matched company_id={company_id} ignoring event_type"
                    ),
                    groups=self._group_articles(fallback_rows),
                )
        return ArticleQueryResult(
            as_of=moment,
            match_mode=MATCH_EXACT,
            fallback_reason=None,
            groups=self._group_articles(rows),
        )

    async def query_candles(
        self,
        *,
        symbol: str,
        start: dt.datetime,
        end: dt.datetime,
        as_of: dt.datetime | None = None,
        timeframe: BarTimeframe = BarTimeframe.MIN_1,
        limit: int = 2000,
    ) -> list[NewsHistoryCandle]:
        """Bars in ``[start, end]`` visible at ``as_of`` (default: no boundary)."""
        ticker = symbol.strip().upper()
        stmt = (
            sa.select(NewsHistoryCandle)
            .where(
                NewsHistoryCandle.symbol == ticker,
                NewsHistoryCandle.timeframe == timeframe.value,
                NewsHistoryCandle.bar_start >= _as_utc(start),
                NewsHistoryCandle.bar_start <= _as_utc(end),
            )
            .order_by(NewsHistoryCandle.bar_start.asc())
            .limit(limit)
        )
        if as_of is not None:
            stmt = stmt.where(NewsHistoryCandle.observed_at <= _as_utc(as_of))
        async with self._database.session() as session:
            return list((await session.execute(stmt)).scalars().all())

    async def coverage(self, *, dataset: str, scope_key: str) -> NewsHistoryCoverage | None:
        async with self._database.session() as session:
            return (
                await session.execute(
                    sa.select(NewsHistoryCoverage).where(
                        NewsHistoryCoverage.dataset == dataset,
                        NewsHistoryCoverage.scope_key == scope_key,
                    )
                )
            ).scalar_one_or_none()

    async def list_coverage(self, *, dataset: str | None = None) -> list[NewsHistoryCoverage]:
        stmt = sa.select(NewsHistoryCoverage).order_by(
            NewsHistoryCoverage.dataset, NewsHistoryCoverage.scope_key
        )
        if dataset is not None:
            stmt = stmt.where(NewsHistoryCoverage.dataset == dataset)
        async with self._database.session() as session:
            return list((await session.execute(stmt)).scalars().all())

    async def latest_run(self, *, dataset: str, scope_key: str) -> NewsHistoryBackfillRun | None:
        async with self._database.session() as session:
            return (
                await session.execute(
                    sa.select(NewsHistoryBackfillRun)
                    .where(
                        NewsHistoryBackfillRun.dataset == dataset,
                        NewsHistoryBackfillRun.scope_key == scope_key,
                    )
                    .order_by(NewsHistoryBackfillRun.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _bounded_window(
        self, start: dt.datetime, end: dt.datetime, caps: BackfillLimits
    ) -> tuple[dt.datetime, dt.datetime]:
        window_start = _as_utc(start)
        window_end = _as_utc(end)
        if window_start >= window_end:
            raise ValueError("start must be before end")
        earliest = window_end - dt.timedelta(days=caps.max_days)
        return max(window_start, earliest), window_end

    @staticmethod
    def _news_scope(symbols: Sequence[str] | None) -> str:
        if not symbols:
            return GLOBAL_SCOPE
        return ",".join(sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()}))

    async def _write_articles(
        self,
        session: AsyncSession,
        articles: Sequence[ArticleInput],
        *,
        ingested_at: dt.datetime,
    ) -> _WriteOutcome:
        written = 0
        duplicates = 0
        watermark: dt.datetime | None = None
        seen: set[tuple[SourceProvider, str]] = set()

        for item in articles:
            published = _as_utc(item.published_at) if item.published_at else None
            observed = item.observed_at or published or ingested_at
            observed = _as_utc(observed)
            watermark = observed if watermark is None else max(watermark, observed)

            if item.provider_item_id is not None:
                key = (item.provider, item.provider_item_id)
                if key in seen:
                    duplicates += 1
                    continue
                seen.add(key)

            existing_id = await self._existing_article_id(session, item)
            if existing_id is not None:
                duplicates += 1
                continue

            # Historical imports never enter the live discovery graph. A Source
            # would claim an actual live receipt at import time, and a NEW event
            # would be picked up by the classification sweep.
            event = await self._ensure_event(session, item, observed, ingested_at)
            company_id = item.company_id or await self._resolve_company(session, item.symbols)

            session.add(
                NewsHistoryArticle(
                    provider=item.provider,
                    provider_item_id=item.provider_item_id,
                    feed=item.feed,
                    headline=item.headline,
                    symbols=list(item.symbols),
                    event_type=item.event_type or (event.event_type if event else None),
                    company_id=company_id,
                    event_id=event.id if event else None,
                    source_id=None,
                    published_at=published,
                    observed_at=observed,
                    ingested_at=ingested_at,
                    content_hash=item.resolved_content_hash,
                    raw_payload=dict(item.raw_payload),
                )
            )
            await session.flush()
            written += 1

        return _WriteOutcome(written=written, duplicates=duplicates, watermark_at=watermark)

    async def _existing_article_id(
        self, session: AsyncSession, item: ArticleInput
    ) -> uuid.UUID | None:
        if item.provider_item_id is not None:
            stmt = sa.select(NewsHistoryArticle.id).where(
                NewsHistoryArticle.provider == item.provider,
                NewsHistoryArticle.provider_item_id == item.provider_item_id,
            )
        else:
            stmt = sa.select(NewsHistoryArticle.id).where(
                NewsHistoryArticle.provider == item.provider,
                NewsHistoryArticle.content_hash == item.resolved_content_hash,
            )
        return (await session.execute(stmt.limit(1))).scalar_one_or_none()

    async def _resolve_company(
        self, session: AsyncSession, symbols: Sequence[str]
    ) -> uuid.UUID | None:
        """Associate one unambiguous provider symbol with a known company.

        Ticker hints are historical context only; they never create a broker
        instrument or an executable EventCompanyImpact.
        """
        normalized = {symbol.strip().upper() for symbol in symbols if symbol.strip()}
        if len(normalized) != 1:
            return None
        matches = (
            (
                await session.execute(
                    sa.select(Company.id)
                    .where(sa.func.upper(Company.primary_symbol) == next(iter(normalized)))
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        return matches[0] if len(matches) == 1 else None

    async def _ensure_event(
        self,
        session: AsyncSession,
        item: ArticleInput,
        observed: dt.datetime,
        ingested_at: dt.datetime,
    ) -> Event | None:
        digest = title_hash(item.headline)
        if not item.headline or not item.headline.strip():
            return None
        event_time = sa.func.coalesce(Event.event_time, Event.first_seen_at)
        cutoff = observed - DEFAULT_EVENT_MATCH_WINDOW
        horizon = observed + DEFAULT_EVENT_MATCH_WINDOW
        existing = (
            await session.execute(
                sa.select(Event)
                .where(
                    Event.title_hash == digest,
                    Event.status == EventStatus.ARCHIVED,
                    sa.exists(
                        sa.select(NewsHistoryArticle.id).where(
                            NewsHistoryArticle.event_id == Event.id
                        )
                    ),
                    event_time >= cutoff,
                    event_time <= horizon,
                )
                .order_by(event_time.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if item.event_type and not existing.event_type:
                existing.event_type = item.event_type
                session.add(existing)
            return existing

        event = Event(
            title=item.headline,
            title_hash=digest,
            summary=None,
            first_seen_at=ingested_at,
            event_time=item.published_at or observed,
            status=EventStatus.ARCHIVED,
            event_type=item.event_type,
            topics=[],
        )
        session.add(event)
        await session.flush()
        return event

    async def _write_candles(
        self,
        session: AsyncSession,
        candles: Sequence[CandleInput],
        *,
        ingested_at: dt.datetime,
    ) -> _WriteOutcome:
        if not candles:
            return _WriteOutcome(written=0, duplicates=0, watermark_at=None)

        rows: list[dict[str, Any]] = []
        watermark: dt.datetime | None = None
        for candle in candles:
            bar_start = _as_utc(candle.bar_start)
            observed = (
                _as_utc(candle.observed_at)
                if candle.observed_at
                else bar_start + _TIMEFRAME_DELTAS[BarTimeframe(candle.timeframe)]
            )
            watermark = observed if watermark is None else max(watermark, observed)
            rows.append(
                {
                    "symbol": candle.symbol.strip().upper(),
                    "timeframe": candle.timeframe,
                    "provider": candle.provider,
                    "feed": candle.feed,
                    "bar_start": bar_start,
                    "observed_at": observed,
                    "ingested_at": ingested_at,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                    "trade_count": candle.trade_count,
                    "vwap": candle.vwap,
                    "raw_payload": dict(candle.raw_payload),
                }
            )

        # PostgreSQL caps one statement at 32,767 bind parameters; a week of
        # consolidated-feed minute bars exceeds that in a single VALUES list.
        written = 0
        for offset in range(0, len(rows), CANDLE_INSERT_BATCH):
            statement = (
                pg_insert(NewsHistoryCandle)
                .values(rows[offset : offset + CANDLE_INSERT_BATCH])
                .on_conflict_do_nothing(index_elements=["symbol", "timeframe", "bar_start", "feed"])
            )
            result = await session.execute(statement)
            written += int(cast("CursorResult[Any]", result).rowcount or 0)
        return _WriteOutcome(
            written=written,
            duplicates=len(rows) - written,
            watermark_at=watermark,
        )

    async def _select_articles(
        self,
        *,
        as_of: dt.datetime,
        company_id: uuid.UUID | None,
        event_type: str | None,
        symbols: Sequence[str] | None,
        limit: int,
    ) -> list[NewsHistoryArticle]:
        stmt = sa.select(NewsHistoryArticle).where(NewsHistoryArticle.observed_at <= as_of)
        if company_id is not None:
            stmt = stmt.where(NewsHistoryArticle.company_id == company_id)
        if event_type is not None:
            stmt = stmt.where(NewsHistoryArticle.event_type == event_type)
        if symbols:
            wanted = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
            if wanted:
                stmt = stmt.where(NewsHistoryArticle.symbols.contains(wanted))
        stmt = stmt.order_by(NewsHistoryArticle.observed_at.desc()).limit(limit)
        async with self._database.session() as session:
            return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    def _group_articles(rows: Sequence[NewsHistoryArticle]) -> tuple[NewsEventGroup, ...]:
        grouped: dict[uuid.UUID | None, list[NewsHistoryArticle]] = {}
        for row in rows:
            grouped.setdefault(row.event_id, []).append(row)
        groups = [
            NewsEventGroup(
                event_id=event_id,
                articles=tuple(sorted(items, key=lambda article: article.observed_at)),
                canonical=event_id is not None,
            )
            for event_id, items in grouped.items()
        ]
        # Most recent event first, then deterministic by the newest article.
        groups.sort(key=lambda group: max(a.observed_at for a in group.articles), reverse=True)
        return tuple(groups)

    async def _start_run(
        self,
        *,
        dataset: str,
        scope_key: str,
        start: dt.datetime,
        end: dt.datetime,
        limits: BackfillLimits,
        started_at: dt.datetime,
    ) -> uuid.UUID:
        async with self._database.transaction() as session:
            run = NewsHistoryBackfillRun(
                dataset=dataset,
                scope_key=scope_key,
                status=RUN_STATUS_RUNNING,
                requested_start=start,
                requested_end=end,
                limits=limits.as_json(),
                started_at=started_at,
            )
            session.add(run)
            await session.flush()
            return run.id

    async def _finish_run(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        *,
        status: str,
        articles_fetched: int,
        bars_fetched: int,
        pages_fetched: int,
        requests_made: int,
        bounded: bool,
        stop_reason: str | None,
        watermark_at: dt.datetime | None,
        finished_at: dt.datetime,
    ) -> None:
        run = await session.get(NewsHistoryBackfillRun, run_id)
        if run is None:  # pragma: no cover - defensive; the id was just created
            return
        run.status = status
        run.articles_fetched = articles_fetched
        run.bars_fetched = bars_fetched
        run.pages_fetched = pages_fetched
        run.requests_made = requests_made
        run.bounded = bounded
        run.stop_reason = stop_reason
        run.watermark_at = watermark_at
        run.finished_at = finished_at
        session.add(run)

    async def _fail_run(self, run_id: uuid.UUID, error: str, failed_at: dt.datetime) -> None:
        async with self._database.transaction() as session:
            run = await session.get(NewsHistoryBackfillRun, run_id)
            if run is None:  # pragma: no cover - defensive
                return
            run.status = RUN_STATUS_FAILED
            run.error = error
            run.finished_at = failed_at
            run.bounded = False
            session.add(run)

    async def _touch_coverage(
        self,
        session: AsyncSession,
        *,
        dataset: str,
        scope_key: str,
        watermark_at: dt.datetime | None,
        attempted_at: dt.datetime,
        is_partial: bool | None,
        detail: str | None,
        article_delta: int = 0,
        bar_delta: int = 0,
        request_delta: int = 0,
        window_start: dt.datetime | None = None,
        window_end: dt.datetime | None = None,
    ) -> None:
        row = (
            await session.execute(
                sa.select(NewsHistoryCoverage).where(
                    NewsHistoryCoverage.dataset == dataset,
                    NewsHistoryCoverage.scope_key == scope_key,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = NewsHistoryCoverage(dataset=dataset, scope_key=scope_key)
            session.add(row)
            await session.flush()

        # A min/max span alone cannot prove continuous coverage. Preserve any
        # earlier partial flag, and flag disjoint completed windows as partial.
        separated = (
            window_start is not None
            and window_end is not None
            and row.covered_start is not None
            and row.covered_end is not None
            and (window_start > row.covered_end or window_end < row.covered_start)
        )

        # A watermark only ever advances.  An older backfill fills a gap; it does
        # not un-see the newer articles already cached.
        if watermark_at is not None and (
            row.watermark_at is None or watermark_at > row.watermark_at
        ):
            row.watermark_at = watermark_at
        if window_start is not None and (
            row.covered_start is None or window_start < row.covered_start
        ):
            row.covered_start = window_start
        if window_end is not None and (row.covered_end is None or window_end > row.covered_end):
            row.covered_end = window_end
        row.article_count += article_delta
        row.bar_count += bar_delta
        row.request_count += request_delta
        # A manual write (``is_partial is None``) must not clear a gap a bounded
        # backfill recorded; only a completed backfill may re-state completeness.
        if is_partial is not None:
            row.is_partial = row.is_partial or is_partial or separated
            row.detail = detail or ("disjoint_coverage_window" if separated else row.detail)
        row.last_attempt_at = attempted_at
        if not is_partial:
            row.last_success_at = attempted_at
        session.add(row)
