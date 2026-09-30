"""The registered 24-month validation universe: bounded backfill and coverage audit.

The universe was fixed before any outcome was inspected: large and small caps
across technology, finance, energy, healthcare, consumer and payments, with
recent winners (NVDA, IONQ), laggards (INTC, PYPL) and a stock taken private
during the window (WBA) so delisted coverage is reported, not hidden.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Callable
from typing import Any

import sqlalchemy as sa

from stockbrain.db.models.news_history import (
    DATASET_CANDLES,
    DATASET_NEWS,
    RUN_STATUS_SUCCEEDED,
    NewsHistoryArticle,
    NewsHistoryBackfillRun,
    NewsHistoryCandle,
    NewsHistoryCoverage,
)
from stockbrain.db.session import Database
from stockbrain.intelligence.news_history import (
    BackfillLimits,
    BackfillReport,
    BarsProvider,
    NewsBackfillClient,
    NewsHistoryCache,
)

REGISTERED_UNIVERSE = ("AAPL", "NVDA", "JPM", "XOM", "JNJ", "TSLA", "INTC", "PYPL", "IONQ", "WBA")
BENCHMARK = "SPY"
WINDOW = dt.timedelta(days=7)
#: One Alpaca minute-bar request per day, plus slack; news pages 50 at a time.
NEWS_LIMITS = BackfillLimits(max_days=7, max_articles=5000, max_bar_pages=100, max_requests=100)
CANDLE_LIMITS = BackfillLimits(max_days=7, max_articles=1, max_bar_pages=9, max_requests=9)


def months_ago(moment: dt.datetime, count: int) -> dt.datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    index = moment.year * 12 + moment.month - 1 - count
    year, month_index = divmod(index, 12)
    month = month_index + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


async def run_validation_coverage(
    database: Database, *, as_of: dt.datetime, imported_by: dt.datetime | None = None
) -> dict[str, Any]:
    """Count cached rows and failed windows without implying performance results.

    ``as_of`` fixes the 24-month window; ``imported_by`` (default ``as_of``)
    bounds what the cache had imported. An offline report run after its window
    passes the run time, since the history is necessarily imported later.
    """
    start, split = months_ago(as_of, 24), months_ago(as_of, 6)
    cutoff = imported_by or as_of
    rows: dict[str, dict[str, Any]] = {}
    async with database.session() as session:
        for symbol in (*REGISTERED_UNIVERSE, BENCHMARK):
            periods: dict[str, Any] = {}
            for name, lower, upper in (
                ("exploration", start, split),
                ("validation", split, as_of),
            ):
                articles = await session.scalar(
                    sa.select(sa.func.count(NewsHistoryArticle.id)).where(
                        NewsHistoryArticle.symbols.contains([symbol]),
                        NewsHistoryArticle.published_at >= lower,
                        NewsHistoryArticle.published_at < upper,
                        NewsHistoryArticle.ingested_at <= cutoff,
                    )
                )
                events = await session.scalar(
                    sa.select(sa.func.count(sa.distinct(NewsHistoryArticle.event_id))).where(
                        NewsHistoryArticle.symbols.contains([symbol]),
                        NewsHistoryArticle.published_at >= lower,
                        NewsHistoryArticle.published_at < upper,
                        NewsHistoryArticle.event_id.is_not(None),
                        NewsHistoryArticle.ingested_at <= cutoff,
                    )
                )
                candles = await session.scalar(
                    sa.select(sa.func.count(NewsHistoryCandle.id)).where(
                        NewsHistoryCandle.symbol == symbol,
                        NewsHistoryCandle.timeframe == "1Min",
                        NewsHistoryCandle.bar_start >= lower,
                        NewsHistoryCandle.bar_start < upper,
                        NewsHistoryCandle.ingested_at <= cutoff,
                    )
                )
                periods[name] = {
                    "articles": int(articles or 0),
                    "linked_events": int(events or 0),
                    "minute_bars": int(candles or 0),
                    "outcomes": "unavailable_without_complete_event_calculation",
                }
            coverage = await session.scalar(
                sa.select(NewsHistoryCoverage).where(
                    NewsHistoryCoverage.dataset == DATASET_CANDLES,
                    NewsHistoryCoverage.scope_key == symbol,
                )
            )
            runs = (
                await session.scalars(
                    sa.select(NewsHistoryBackfillRun).where(
                        NewsHistoryBackfillRun.scope_key == symbol,
                        NewsHistoryBackfillRun.started_at <= cutoff,
                        NewsHistoryBackfillRun.status.in_(("FAILED", "PARTIAL")),
                    )
                )
            ).all()
            rows[symbol] = {
                **periods,
                "coverage": (
                    {
                        "covered_start": coverage.covered_start.isoformat()
                        if coverage.covered_start
                        else None,
                        "covered_end": coverage.covered_end.isoformat()
                        if coverage.covered_end
                        else None,
                        "partial": coverage.is_partial,
                        "detail": coverage.detail,
                    }
                    if coverage is not None
                    and coverage.last_attempt_at
                    and coverage.last_attempt_at <= cutoff
                    else {"status": "unknown_at_as_of"}
                ),
                "failed_or_partial_runs": [
                    {
                        "status": run.status,
                        "requested_start": run.requested_start.isoformat()
                        if run.requested_start
                        else None,
                        "requested_end": run.requested_end.isoformat()
                        if run.requested_end
                        else None,
                        "reason": run.stop_reason or run.error,
                    }
                    for run in runs
                ],
            }
    return {
        "as_of": as_of.isoformat(),
        "window_start": start.isoformat(),
        "validation_start": split.isoformat(),
        "window_end": as_of.isoformat(),
        "universe": list(REGISTERED_UNIVERSE),
        "benchmark": BENCHMARK,
        "symbols": rows,
        "performance_status": "not_calculated_from_coverage_counts",
    }


async def _completed(
    database: Database, dataset: str, scope: str, start: dt.datetime, end: dt.datetime
) -> bool:
    async with database.session() as session:
        found = await session.scalar(
            sa.select(NewsHistoryBackfillRun.id)
            .where(
                NewsHistoryBackfillRun.dataset == dataset,
                NewsHistoryBackfillRun.scope_key == scope,
                NewsHistoryBackfillRun.requested_start == start,
                NewsHistoryBackfillRun.requested_end == end,
                NewsHistoryBackfillRun.status == RUN_STATUS_SUCCEEDED,
            )
            .limit(1)
        )
    return found is not None


async def backfill_universe(
    database: Database,
    *,
    as_of: dt.datetime,
    news: NewsBackfillClient,
    bars: BarsProvider,
    symbols: tuple[str, ...] = REGISTERED_UNIVERSE,
    progress: Callable[[BackfillReport], None] | None = None,
) -> dict[str, int]:
    """Fill 24 months in fixed seven-day windows; rerunning resumes.

    A window already recorded as SUCCEEDED is skipped. PARTIAL and FAILED runs
    stay in the audit table and are retried on the next invocation, so an
    incomplete request is never silently discarded.
    """
    start = months_ago(as_of, 24)
    cache = NewsHistoryCache(database)
    counts = {"skipped": 0, "succeeded": 0, "partial_or_failed": 0}
    cursor = start
    while cursor < as_of:
        window_end = min(cursor + WINDOW, as_of)
        jobs: list[tuple[str, str]] = [(DATASET_NEWS, symbol) for symbol in symbols]
        jobs += [(DATASET_CANDLES, symbol) for symbol in (*symbols, BENCHMARK)]
        for dataset, symbol in jobs:
            if await _completed(database, dataset, symbol, cursor, window_end):
                counts["skipped"] += 1
                continue
            try:
                if dataset == DATASET_NEWS:
                    report = await cache.backfill_news(
                        start=cursor,
                        end=window_end,
                        symbols=(symbol,),
                        limits=NEWS_LIMITS,
                        client=news,
                    )
                else:
                    report = await cache.backfill_candles(
                        symbol=symbol,
                        start=cursor,
                        end=window_end,
                        limits=CANDLE_LIMITS,
                        provider=bars,
                    )
            except Exception:
                # The run row already records FAILED with its error.
                counts["partial_or_failed"] += 1
                continue
            counts["succeeded" if not report.partial else "partial_or_failed"] += 1
            if progress is not None:
                progress(report)
        cursor = window_end
    return counts
