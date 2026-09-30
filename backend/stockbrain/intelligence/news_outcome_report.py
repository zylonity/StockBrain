"""Offline 24-month news-and-candle outcome report for the registered universe.

Read-only: it reads the historical cache and prints JSON. Nothing here touches
live research, proposals or the broker.

Pre-registered choices, fixed before any outcome was inspected:

* Universe: ``REGISTERED_UNIVERSE``; benchmark SPY.
* Window: the 24 calendar months before ``as_of``; the earlier 18 months are
  exploration, the latest 6 an untouched chronological validation holdout.
* One observation per archived grouping event (duplicate stories count once).
  Articles tagged with more than ``MAX_TAGGED_SYMBOLS`` tickers are market
  round-ups and are counted as excluded, not measured.
* Entry: simulated arrival at publication + 1, 5, 15 or 60 minutes (the
  historical cache has no actual live receipt), then the next minute bar.
* Costs: a 10 bps round-trip spread proxy. FX is not applied (all USD).
* Thresholds for "first meaningful move": +0.5%, +1%, +2% market-relative.
* Event type: a keyword rule on the headline, reported as ``headline_type``
  and never used by live exact-type matching.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import datetime as dt
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any

import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.news_history import NewsHistoryArticle, NewsHistoryCandle
from stockbrain.db.session import Database
from stockbrain.intelligence.news_outcome_coverage import (
    BENCHMARK,
    REGISTERED_UNIVERSE,
    months_ago,
    run_validation_coverage,
)
from stockbrain.intelligence.news_outcome_validation import (
    MIN_COHORT,
    SESSION_ZONE,
    THRESHOLDS,
    HistoricalEvent,
    compare_deadline_exit_to_hold,
    derive_catalyst_deadline,
    threshold_timing,
)
from stockbrain.market_data.base import Bar
from stockbrain.market_data.news_outcomes import (
    ConfoundingPolicy,
    NewsOutcomeInputs,
    OutcomeStatus,
    compute_news_outcome,
)

DELAYS_MINUTES = (1, 5, 15, 60)
PRIMARY_DELAY = 5
HORIZONS = ("1h", "1d", "5d")
SPREAD_BPS = Decimal("10")
ROUND_TRIP_COST = SPREAD_BPS / Decimal(10_000)
MAX_TAGGED_SYMBOLS = 3
PRIOR_MOVE_BUCKETS: tuple[tuple[str, Decimal | None, Decimal | None], ...] = (
    ("<= -1%", None, Decimal("-0.01")),
    ("-1% to +0.5%", Decimal("-0.01"), Decimal("0.005")),
    ("+0.5% to +1%", Decimal("0.005"), Decimal("0.01")),
    ("+1% to +2%", Decimal("0.01"), Decimal("0.02")),
    ("> +2%", Decimal("0.02"), None),
)
HEADLINE_TYPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "EARNINGS",
        re.compile(r"\b(earnings|quarterly|results|eps|q[1-4]\b|revenue|profit)", re.I),
    ),
    ("GUIDANCE", re.compile(r"\b(guidance|outlook|forecast|sees)\b", re.I)),
    (
        "ANALYST",
        re.compile(
            r"\b(upgrade|downgrade|price target|rating|initiat|overweight|underweight)", re.I
        ),
    ),
    ("M_AND_A", re.compile(r"\b(acqui|merger|buyout|takeover|take-private|to buy)\w*", re.I)),
    (
        "LEGAL_REGULATORY",
        re.compile(
            r"\b(lawsuit|sues|probe|investigation|antitrust|fda|regulator|fine[sd]?)\b", re.I
        ),
    ),
)


def headline_type(headline: str | None) -> str:
    for name, pattern in HEADLINE_TYPES:
        if headline and pattern.search(headline):
            return name
    return "OTHER"


@dataclass(frozen=True, slots=True)
class _Event:
    event_id: str
    symbol: str
    published_at: dt.datetime
    headline: str
    kind: str
    articles: int


@dataclass(slots=True)
class _Series:
    bars: list[Bar]
    starts: list[dt.datetime]

    def window(self, start: dt.datetime, end: dt.datetime) -> list[Bar]:
        return self.bars[
            bisect.bisect_left(self.starts, start) : bisect.bisect_right(self.starts, end)
        ]


def _wilson(positive: int, total: int) -> tuple[str, str] | None:
    if total == 0:
        return None
    z = 1.96
    phat = positive / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denominator
    return (f"{max(0.0, centre - margin):.3f}", f"{min(1.0, centre + margin):.3f}")


def _distribution(values: Sequence[Decimal], unavailable: int) -> dict[str, Any]:
    positive = sum(value > 0 for value in values)
    usable = len(values)
    sparse = usable < MIN_COHORT
    ordered = sorted(values)

    def quantile(q: float) -> str | None:
        if sparse or not ordered:
            return None
        return str(
            ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))].quantize(
                Decimal("0.0001")
            )
        )

    return {
        "usable": usable,
        "unavailable": unavailable,
        "positive": positive,
        "flat": sum(value == 0 for value in values),
        "negative": sum(value < 0 for value in values),
        "p_positive": f"{positive / usable:.3f}" if usable and not sparse else None,
        "p_positive_95ci": _wilson(positive, usable) if not sparse else None,
        "median_net": str(median(values).quantize(Decimal("0.0001")))
        if values and not sparse
        else None,
        "p10_net": quantile(0.10),
        "p90_net": quantile(0.90),
        "sparse": sparse,
    }


async def _load_series(
    database: Database, symbol: str, start: dt.datetime, end: dt.datetime
) -> _Series:
    async with database.session() as session:
        rows = (
            await session.execute(
                sa.select(
                    NewsHistoryCandle.bar_start,
                    NewsHistoryCandle.open,
                    NewsHistoryCandle.high,
                    NewsHistoryCandle.low,
                    NewsHistoryCandle.close,
                    NewsHistoryCandle.volume,
                    NewsHistoryCandle.feed,
                )
                .where(
                    NewsHistoryCandle.symbol == symbol,
                    NewsHistoryCandle.timeframe == "1Min",
                    NewsHistoryCandle.bar_start >= start,
                    NewsHistoryCandle.bar_start < end,
                )
                .order_by(NewsHistoryCandle.bar_start)
            )
        ).all()
    bars = [
        Bar(
            symbol=symbol,
            timestamp=row[0],
            open=row[1],
            high=row[2],
            low=row[3],
            close=row[4],
            volume=row[5],
            feed=row[6],
        )
        for row in rows
    ]
    return _Series(bars, [bar.timestamp for bar in bars])


async def _load_events(
    database: Database, symbol: str, start: dt.datetime, end: dt.datetime
) -> tuple[list[_Event], Counter[str]]:
    excluded: Counter[str] = Counter()
    async with database.session() as session:
        rows = (
            await session.execute(
                sa.select(
                    NewsHistoryArticle.event_id,
                    NewsHistoryArticle.published_at,
                    NewsHistoryArticle.headline,
                    NewsHistoryArticle.symbols,
                )
                .where(
                    NewsHistoryArticle.symbols.contains([symbol]),
                    NewsHistoryArticle.published_at >= start,
                    NewsHistoryArticle.published_at < end,
                )
                .order_by(NewsHistoryArticle.published_at)
            )
        ).all()
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        if row.event_id is None:
            excluded["no_headline_group"] += 1
            continue
        grouped[str(row.event_id)].append(row)
    events: list[_Event] = []
    for event_id, articles in grouped.items():
        first = min(articles, key=lambda row: row.published_at)
        if len(first.symbols or ()) > MAX_TAGGED_SYMBOLS:
            excluded["multi_ticker_roundup"] += 1
            continue
        events.append(
            _Event(
                event_id=event_id,
                symbol=symbol,
                published_at=first.published_at,
                headline=first.headline or "",
                kind=headline_type(first.headline),
                articles=len(articles),
            )
        )
    events.sort(key=lambda item: item.published_at)
    return events, excluded


def _relative_at_session_close(item: HistoricalEvent, sessions_after_entry: int) -> Decimal | None:
    """Market-relative return from entry to the close of the k-th later session."""
    entry = item.outcome.entry
    benchmark_entry = item.outcome.publication_to_entry.benchmark_entry
    if entry is None or benchmark_entry is None or entry.price <= 0 or benchmark_entry.price <= 0:
        return None
    entry_date = entry.timestamp.astimezone(SESSION_ZONE).date()
    dates = sorted(
        {
            bar.timestamp.astimezone(SESSION_ZONE).date()
            for bar in item.stock_bars
            if bar.timestamp.astimezone(SESSION_ZONE).date() > entry_date
        }
    )
    if sessions_after_entry == 0:
        target = entry_date
    elif len(dates) >= sessions_after_entry:
        target = dates[sessions_after_entry - 1]
    else:
        return None
    benchmark = {bar.timestamp: bar for bar in item.benchmark_bars}
    for bar in reversed(item.stock_bars):
        if bar.timestamp.astimezone(SESSION_ZONE).date() != target:
            continue
        market = benchmark.get(bar.timestamp)
        if market is None or bar.timestamp.astimezone(SESSION_ZONE).time() > dt.time(16, 0):
            continue
        return (bar.close / entry.price - 1) - (market.close / benchmark_entry.price - 1)
    return None


def _evaluate(
    events: list[_Event], stock: _Series, market: _Series, as_of: dt.datetime
) -> dict[int, list[tuple[_Event, HistoricalEvent]]]:
    results: dict[int, list[tuple[_Event, HistoricalEvent]]] = {
        delay: [] for delay in DELAYS_MINUTES
    }
    publications = [event.published_at for event in events]
    for event in events:
        start = event.published_at - dt.timedelta(days=4)
        end = min(as_of, event.published_at + dt.timedelta(days=10))
        stock_bars = stock.window(start, end)
        market_bars = market.window(start, end)
        confounders = tuple(
            moment
            for moment in publications[
                bisect.bisect_right(publications, event.published_at) : bisect.bisect_right(
                    publications, end
                )
            ]
        )
        for delay in DELAYS_MINUTES:
            outcome = compute_news_outcome(
                NewsOutcomeInputs(
                    event_id=event.event_id,
                    published_at=event.published_at,
                    as_of=as_of,
                    stock_bars=stock_bars,
                    benchmark_bars=market_bars,
                    latency=dt.timedelta(minutes=delay),
                    spread_bps=SPREAD_BPS,
                    instrument_currency="USD",
                    benchmark_currency="USD",
                    confounding_publications=confounders,
                    confounding_policy=ConfoundingPolicy.FLAG,
                )
            )
            results[delay].append(
                (
                    event,
                    HistoricalEvent(
                        canonical_id=event.event_id,
                        symbol=event.symbol,
                        event_type=event.kind,
                        published_at=event.published_at,
                        # Simulated arrival, labelled as such in the report.
                        live_received_at=event.published_at + dt.timedelta(minutes=delay),
                        outcome=outcome,
                        stock_bars=stock_bars,
                        benchmark_bars=market_bars,
                        article_count=event.articles,
                    ),
                )
            )
    return results


def _period_report(rows: list[tuple[_Event, HistoricalEvent]]) -> dict[str, Any]:
    status: Counter[str] = Counter()
    confounded = 0
    horizon_values: dict[str, list[Decimal]] = {name: [] for name in HORIZONS}
    horizon_unavailable: Counter[str] = Counter()
    for _event, item in rows:
        status[item.outcome.status.value] += 1
        for name in HORIZONS:
            result = item.outcome.horizon(name)
            if (
                result is None
                or result.status is not OutcomeStatus.OK
                or result.market_relative_return_net is None
            ):
                horizon_unavailable[name] += 1
            else:
                horizon_values[name].append(result.market_relative_return_net)
                confounded += int(name == "1d" and result.confounded)
    return {
        "events": len(rows),
        "entry_status": dict(status),
        "one_day_windows_with_later_company_news": confounded,
        "net_market_relative": {
            name: _distribution(values, horizon_unavailable[name])
            for name, values in horizon_values.items()
        },
    }


def _timing_report(rows: list[tuple[_Event, HistoricalEvent]]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for threshold in THRESHOLDS:
        phases: Counter[str] = Counter()
        clock: list[int] = []
        trading: list[int] = []
        for _event, item in rows:
            timing = threshold_timing(item, threshold, basis="publication")
            phases[timing.phase if timing.status == "crossed" else timing.status] += 1
            if timing.clock_seconds is not None:
                clock.append(timing.clock_seconds)
            if timing.trading_seconds is not None:
                trading.append(timing.trading_seconds)
        report[f"+{threshold * 100}%"] = {
            "outcomes": dict(phases),
            "median_clock_minutes_to_cross": round(median(clock) / 60, 1)
            if len(clock) >= MIN_COHORT
            else None,
            "median_observed_trading_minutes_to_cross": round(median(trading) / 60, 1)
            if len(trading) >= MIN_COHORT
            else None,
            "crossed_with_trading_time_known": len(trading),
        }
    return report


def _arrival_cohorts(rows: list[tuple[_Event, HistoricalEvent]]) -> dict[str, Any]:
    """All events at this simulated arrival, bucketed by the move already made."""
    buckets: dict[str, dict[str, list[Decimal]]] = {
        label: {name: [] for name in HORIZONS} for label, _low, _high in PRIOR_MOVE_BUCKETS
    }
    unavailable: dict[str, Counter[str]] = {
        label: Counter() for label, _l, _h in PRIOR_MOVE_BUCKETS
    }
    unknown_prior = 0
    for _event, item in rows:
        move = item.outcome.publication_to_entry.market_relative_return
        if move is None:
            unknown_prior += 1
            continue
        label = next(
            name
            for name, low, high in PRIOR_MOVE_BUCKETS
            if (low is None or move > low) and (high is None or move <= high)
        )
        for name in HORIZONS:
            result = item.outcome.horizon(name)
            if (
                result is None
                or result.status is not OutcomeStatus.OK
                or result.market_relative_return_net is None
            ):
                unavailable[label][name] += 1
            else:
                buckets[label][name].append(result.market_relative_return_net)
    return {
        "prior_move_unknown": unknown_prior,
        "buckets": {
            label: {
                name: _distribution(values, unavailable[label][name])
                for name, values in horizons.items()
            }
            for label, horizons in buckets.items()
        },
    }


def _deadline_exit_test(
    exploration: list[tuple[_Event, HistoricalEvent]],
    validation: list[tuple[_Event, HistoricalEvent]],
    buffer_sessions: int,
) -> dict[str, Any]:
    """Derive deadlines on exploration only; test exit-vs-hold on validation.

    Every validation event of a type is included (the bot's actual BUY choices
    are not reproducible offline). At the deadline close an event below the
    threshold exits; otherwise it holds to five sessions. Both legs carry one
    round-trip cost.
    """
    by_type: dict[str, list[HistoricalEvent]] = defaultdict(list)
    for _event, item in exploration:
        by_type[item.event_type].append(item)
    report: dict[str, Any] = {}
    for kind in sorted({item.event_type for _e, item in validation} | set(by_type)):
        evidence = derive_catalyst_deadline(by_type.get(kind, []), buffer_sessions=buffer_sessions)
        entry: dict[str, Any] = {
            "exploration_sample": evidence.sample_size,
            "exploration_responders": evidence.responders,
            "exploration_nonresponses": evidence.nonresponses,
            "exploration_unknown": evidence.unknown,
            "deadline_sessions_after_entry": evidence.deadline_sessions_after_entry,
            "no_deadline_reason": evidence.reason,
        }
        deadline = evidence.deadline_sessions_after_entry
        if deadline is not None:
            pairs: list[tuple[Decimal, Decimal]] = []
            exits = 0
            for _event, item in validation:
                if item.event_type != kind:
                    continue
                hold = item.outcome.horizon("5d")
                at_deadline = _relative_at_session_close(item, deadline)
                if hold is None or hold.market_relative_return_net is None or at_deadline is None:
                    continue
                if deadline >= 5 or at_deadline >= evidence.threshold:
                    strategy = hold.market_relative_return_net
                else:
                    strategy = at_deadline - ROUND_TRIP_COST
                    exits += 1
                pairs.append((strategy, hold.market_relative_return_net))
            comparison = compare_deadline_exit_to_hold(pairs)
            entry["validation"] = {
                "pairs": comparison.sample_size,
                "deadline_exits": exits,
                "deadline_rule_better": comparison.deadline_exit_better,
                "hold_better": comparison.thesis_hold_better,
                "tied": comparison.tied,
                "median_net_advantage_of_rule": str(comparison.median_net_advantage)
                if comparison.median_net_advantage is not None
                else None,
                "automatic_exit_eligible": comparison.automatic_exit_eligible,
            }
        report[kind] = entry
    return report


async def build_report(
    database: Database, *, as_of: dt.datetime, buffer_sessions: int = 1
) -> dict[str, Any]:
    start, split = months_ago(as_of, 24), months_ago(as_of, 6)
    load_start = start - dt.timedelta(days=5)
    market = await _load_series(database, BENCHMARK, load_start, as_of)
    per_symbol: dict[str, Any] = {}
    pooled: dict[str, dict[int, list[tuple[_Event, HistoricalEvent]]]] = {
        "exploration": defaultdict(list),
        "validation": defaultdict(list),
    }
    pooled_ids: set[str] = set()
    for symbol in REGISTERED_UNIVERSE:
        events, excluded = await _load_events(database, symbol, start, as_of)
        stock = await _load_series(database, symbol, load_start, as_of)
        evaluated = _evaluate(events, stock, market, as_of)
        primary = evaluated[PRIMARY_DELAY]
        per_symbol[symbol] = {
            "events": len(events),
            "excluded": dict(excluded),
            "minute_bars": len(stock.bars),
            "first_bar": stock.bars[0].timestamp.isoformat() if stock.bars else None,
            "last_bar": stock.bars[-1].timestamp.isoformat() if stock.bars else None,
            "exploration": _period_report([row for row in primary if row[0].published_at < split]),
            "validation": _period_report([row for row in primary if row[0].published_at >= split]),
        }
        # An event tagged with two or three universe tickers is pooled once,
        # under the first symbol that reaches it; per-symbol sections keep it.
        fresh = {event.event_id for event in events} - pooled_ids
        pooled_ids.update(fresh)
        for delay, rows in evaluated.items():
            for row in rows:
                if row[0].event_id not in fresh:
                    continue
                period = "exploration" if row[0].published_at < split else "validation"
                pooled[period][delay].append(row)
    periods: dict[str, Any] = {}
    for period, by_delay in pooled.items():
        primary_rows = by_delay[PRIMARY_DELAY]
        periods[period] = {
            "overall": _period_report(primary_rows),
            "by_headline_type": {
                kind: _period_report([row for row in primary_rows if row[0].kind == kind])
                for kind in sorted({row[0].kind for row in primary_rows})
            },
            "time_to_first_move_from_publication": _timing_report(primary_rows),
            "arrival_cohorts_by_delay_minutes": {
                str(delay): _arrival_cohorts(rows) for delay, rows in sorted(by_delay.items())
            },
        }
    coverage = await run_validation_coverage(database, as_of=as_of)
    return {
        "as_of": as_of.isoformat(),
        "window_start": start.isoformat(),
        "validation_start": split.isoformat(),
        "universe": list(REGISTERED_UNIVERSE),
        "benchmark": BENCHMARK,
        "assumptions": {
            "entry": (
                "simulated arrival at publication + delay, then the next minute bar; "
                "history has no actual live receipt"
            ),
            "primary_delay_minutes": PRIMARY_DELAY,
            "round_trip_spread_proxy_bps": str(SPREAD_BPS),
            "fx": "not applied; all instruments and benchmark are USD",
            "event_type": "headline keyword rule; not the live classifier",
            "min_cohort_for_statistics": MIN_COHORT,
        },
        "periods": periods,
        "deadline_exit_vs_hold": _deadline_exit_test(
            pooled["exploration"][PRIMARY_DELAY],
            pooled["validation"][PRIMARY_DELAY],
            buffer_sessions,
        ),
        "per_symbol": per_symbol,
        "coverage": coverage["symbols"],
    }


def _aware(value: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return moment.astimezone(dt.UTC)


async def _main(args: argparse.Namespace) -> None:
    database = Database(Settings(database_url=args.database_url))
    try:
        report = await build_report(database, as_of=args.as_of)
    finally:
        await database.dispose()
    print(json.dumps(report, indent=2, default=str))  # noqa: T201


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--as-of", type=_aware, required=True)
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()


__all__ = ["build_report", "headline_type"]
