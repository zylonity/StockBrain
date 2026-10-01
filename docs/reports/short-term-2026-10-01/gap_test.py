"""Test 1: do gaps with overnight news continue, and gaps without news fade?

Pre-registered (fixed before looking at results):
- Gap: first regular-session open vs the prior regular close, minus SPY's same gap,
  >= +2% (up) or <= -2% (down). Sensitivity: 4%.
- News: >= 1 story for the symbol (<= 3 tagged tickers) published after the prior
  16:00 ET close and before the 09:30 open.
- Entry: open of the 09:35 bar (first feasible price). Exits: close of the entry day
  (d0) and closes 1, 3, 5 sessions later.
- Long only. Costs per round trip: 0.10% (spread) and 0.40% (spread + GBP<->USD FX).
- Exploration before 2026-03-30; validation after. Stats per event and per day.
"""

import asyncio
import bisect
import datetime as dt
import json
import statistics
import sys
from collections import defaultdict
from zoneinfo import ZoneInfo

import sqlalchemy as sa

sys.path.insert(0, "backend")
from stockbrain.config import Settings  # noqa: E402
from stockbrain.db.models.news_history import NewsHistoryArticle, NewsHistoryCandle  # noqa: E402
from stockbrain.db.session import Database  # noqa: E402

NY = ZoneInfo("America/New_York")
UNIVERSE = ("AAPL", "NVDA", "JPM", "XOM", "JNJ", "TSLA", "INTC", "PYPL", "IONQ", "WBA")
SPLIT = dt.date(2026, 3, 30)
HOLDS = (0, 1, 3, 5)
COSTS = {"cost_0.10%": 0.0010, "cost_0.40%": 0.0040}


async def sessions(db, symbol):
    """date -> (open at 09:30, open at 09:35 bar, close at last RTH bar)."""
    async with db.session() as s:
        rows = (
            await s.execute(
                sa.select(NewsHistoryCandle.bar_start, NewsHistoryCandle.open, NewsHistoryCandle.close)
                .where(NewsHistoryCandle.symbol == symbol, NewsHistoryCandle.timeframe == "1Min")
                .order_by(NewsHistoryCandle.bar_start)
            )
        ).all()
    days = defaultdict(list)
    for t, o, c in rows:
        local = t.astimezone(NY)
        if dt.time(9, 30) <= local.time() < dt.time(16, 0):
            days[local.date()].append((local.time(), float(o), float(c)))
    out = {}
    for day, bars in days.items():
        first = bars[0]
        if first[0] > dt.time(9, 35):
            continue  # opening bars missing: no reliable gap or entry
        entry = next((b for b in bars if b[0] >= dt.time(9, 35)), None)
        if entry is None or bars[-1][0] < dt.time(15, 50):
            continue
        out[day] = (first[1], entry[1], bars[-1][2])
    return out


async def news_times(db, symbol):
    async with db.session() as s:
        rows = (
            await s.execute(
                sa.select(NewsHistoryArticle.published_at, NewsHistoryArticle.symbols)
                .where(NewsHistoryArticle.symbols.contains([symbol]), NewsHistoryArticle.published_at.is_not(None))
                .order_by(NewsHistoryArticle.published_at)
            )
        ).all()
    return [p for p, sy in rows if len(sy or ()) <= 3]


def summarise(values):
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values) * 100, 2),
        "median": round(statistics.median(values) * 100, 2),
        "p_pos": round(sum(v > 0 for v in values) / len(values), 3),
    }


async def main():
    db = Database(Settings(database_url=sys.argv[1]))
    threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.02
    spy = await sessions(db, "SPY")
    # bucket -> period -> hold -> list of (day, raw_ret, rel_ret)
    results = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    counts = defaultdict(int)
    for symbol in UNIVERSE:
        stock = await sessions(db, symbol)
        pubs = await news_times(db, symbol)
        days = sorted(set(stock) & set(spy))
        for i in range(1, len(days)):
            prev, day = days[i - 1], days[i]
            s_open, s_entry, _ = stock[day]
            m_open, m_entry, _ = spy[day]
            gap = (s_open / stock[prev][2] - 1) - (m_open / spy[prev][2] - 1)
            if abs(gap) < threshold:
                continue
            close_prev = dt.datetime.combine(prev, dt.time(16, 0), NY)
            open_now = dt.datetime.combine(day, dt.time(9, 30), NY)
            has_news = bisect.bisect_left(pubs, open_now) > bisect.bisect_right(pubs, close_prev)
            bucket = f"gap_{'up' if gap > 0 else 'down'}_{'news' if has_news else 'no_news'}"
            period = "exploration" if day < SPLIT else "validation"
            counts[f"{bucket}_{period}"] += 1
            for hold in HOLDS:
                if i + hold >= len(days):
                    continue
                exit_day = days[i + hold]
                raw = stock[exit_day][2] / s_entry - 1
                rel = raw - (spy[exit_day][2] / m_entry - 1)
                results[bucket][period][hold].append((day, raw, rel))
    await db.dispose()

    report = {"threshold": threshold, "events": dict(sorted(counts.items())), "results": {}}
    for bucket in sorted(results):
        report["results"][bucket] = {}
        for period in ("exploration", "validation"):
            rows = {}
            for hold in HOLDS:
                data = results[bucket][period][hold]
                entry = {}
                for label, cost in COSTS.items():
                    entry[label] = {
                        "raw": summarise([r - cost for _d, r, _x in data]),
                        "vs_spy": summarise([x - cost for _d, _r, x in data]),
                    }
                # one observation per day, so clustered same-day events count once
                per_day = defaultdict(list)
                for d, r, _x in data:
                    per_day[d].append(r - COSTS["cost_0.40%"])
                entry["days"] = len(per_day)
                entry["per_day_mean_raw_cost_0.40%"] = (
                    round(statistics.fmean(statistics.fmean(v) for v in per_day.values()) * 100, 2)
                    if per_day
                    else None
                )
                rows[f"hold_{hold}d"] = entry
            report["results"][bucket][period] = rows
    print(json.dumps(report, indent=1))


asyncio.run(main())
