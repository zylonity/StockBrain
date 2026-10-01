"""Exploratory: find market-relative spikes, look back for articles, then check forwards.

Read-only against the local report DB. Spike = the stock beats SPY by >= 2% within
30 regular-session minutes (intraday), or opens >= 2% relative above the prior
regular close (gap). One of each kind per symbol-day and direction.
"""

import asyncio
import bisect
import datetime as dt
import json
import re
import sys
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

import numpy as np
import sqlalchemy as sa

sys.path.insert(0, "backend")
from stockbrain.config import Settings  # noqa: E402
from stockbrain.db.models.news_history import NewsHistoryArticle, NewsHistoryCandle  # noqa: E402
from stockbrain.db.session import Database  # noqa: E402

NY = ZoneInfo("America/New_York")
UNIVERSE = ("AAPL", "NVDA", "JPM", "XOM", "JNJ", "TSLA", "INTC", "PYPL", "IONQ", "WBA")
SPLIT = dt.datetime(2026, 3, 30, tzinfo=dt.UTC)
THRESH = 0.02
WINDOW = 30
LOOKBACK = dt.timedelta(minutes=60)
STOP = set(
    "the a an and or of to in on for with at by from as is are be its it this that after "
    "over into new says said stock stocks shares share inc corp co company amid than more "
    "why what how here you your will could may can not has have was were about".split()
)
MOVE_WORDS = re.compile(r"\b(soar|jump|surge|rall|spike|climb|rise|rising|gain|up \d|higher|pop|rip)", re.I)


async def series(db, symbol):
    async with db.session() as s:
        rows = (
            await s.execute(
                sa.select(NewsHistoryCandle.bar_start, NewsHistoryCandle.open, NewsHistoryCandle.close)
                .where(NewsHistoryCandle.symbol == symbol, NewsHistoryCandle.timeframe == "1Min")
                .order_by(NewsHistoryCandle.bar_start)
            )
        ).all()
    out = {}
    for t, o, c in rows:
        local = t.astimezone(NY)
        if dt.time(9, 30) <= local.time() < dt.time(16, 0):
            out[t] = (float(o), float(c))
    return out


async def articles(db, symbol):
    async with db.session() as s:
        rows = (
            await s.execute(
                sa.select(NewsHistoryArticle.published_at, NewsHistoryArticle.headline, NewsHistoryArticle.symbols)
                .where(NewsHistoryArticle.symbols.contains([symbol]), NewsHistoryArticle.published_at.is_not(None))
                .order_by(NewsHistoryArticle.published_at)
            )
        ).all()
    return [(p, h or "", len(sy or ())) for p, h, sy in rows]


def spikes(stock, market):
    times = sorted(set(stock) & set(market))
    days = defaultdict(list)
    for t in times:
        days[t.astimezone(NY).date()].append(t)
    found = []  # (onset, kind, direction, size)
    prev_close = None
    for day in sorted(days):
        ts = days[day]
        s = np.array([stock[t][1] for t in ts])
        m = np.array([market[t][1] for t in ts])
        if prev_close is not None:
            ps, pm = prev_close
            gap = (stock[ts[0]][0] / ps - 1) - (market[ts[0]][0] / pm - 1)
            if abs(gap) >= THRESH:
                found.append((ts[0], "gap", 1 if gap > 0 else -1, gap))
        if len(ts) > WINDOW:
            rel = (s[WINDOW:] / s[:-WINDOW]) - (m[WINDOW:] / m[:-WINDOW])
            for direction in (1, -1):
                hits = np.nonzero(rel * direction >= THRESH)[0]
                if len(hits):
                    i = int(hits[0])
                    found.append((ts[i] + dt.timedelta(minutes=1), "intraday", direction, float(rel[i])))
        prev_close = (s[-1], m[-1])
    return found


def tokens(headline):
    return {w for w in re.findall(r"[a-z][a-z\-]{2,}", headline.lower()) if w not in STOP}


async def main():
    db = Database(Settings(database_url=sys.argv[1]))
    market = await series(db, "SPY")
    summary = Counter()
    before_move_words = after_move_words = before_n = after_n = 0
    per_article = []  # (period, tokens, followed_by_up_spike)
    examples = []
    for symbol in UNIVERSE:
        stock = await series(db, symbol)
        arts = [a for a in await articles(db, symbol) if a[2] <= 3]
        pubs = [a[0] for a in arts]
        found = spikes(stock, market)
        up_onsets = sorted(o for o, _k, d, _s in found if d == 1)
        for onset, kind, direction, size in found:
            lo = onset - (dt.timedelta(hours=17, minutes=30) if kind == "gap" else LOOKBACK)
            before = arts[bisect.bisect_left(pubs, lo) : bisect.bisect_left(pubs, onset)]
            after = arts[bisect.bisect_left(pubs, onset) : bisect.bisect_right(pubs, onset + LOOKBACK)]
            key = f"{kind}_{'up' if direction > 0 else 'down'}"
            summary[f"{key}_total"] += 1
            summary[f"{key}_{'story_before' if before else 'story_only_after' if after else 'no_story'}"] += 1
            before_n += len(before)
            after_n += len(after)
            before_move_words += sum(bool(MOVE_WORDS.search(h)) for _p, h, _n in before)
            after_move_words += sum(bool(MOVE_WORDS.search(h)) for _p, h, _n in after)
            if direction > 0 and before and len(examples) < 12 and size > 0.05:
                examples.append((symbol, kind, onset.isoformat(), round(size, 3), [h[:90] for _p, h, _n in before[-2:]]))
        # forward: every article -> was an up-spike onset within the next 60 min (intraday),
        # or, if published outside the session, a gap-up at the next open?
        for p, h, _n in arts:
            i = bisect.bisect_right(up_onsets, p)
            nxt = up_onsets[i] if i < len(up_onsets) else None
            local = p.astimezone(NY)
            in_session = local.weekday() < 5 and dt.time(9, 30) <= local.time() < dt.time(16, 0)
            horizon = LOOKBACK if in_session else dt.timedelta(hours=18)
            hit = nxt is not None and nxt - p <= horizon
            per_article.append(("exploration" if p < SPLIT else "validation", tokens(h), hit))
    await db.dispose()

    base = {}
    for period in ("exploration", "validation"):
        rows = [r for r in per_article if r[0] == period]
        base[period] = (len(rows), sum(r[2] for r in rows))
    counts = {period: defaultdict(lambda: [0, 0]) for period in base}
    for period, toks, hit in per_article:
        for w in toks:
            counts[period][w][0] += 1
            counts[period][w][1] += hit
    brate = {p: base[p][1] / base[p][0] for p in base}
    candidates = [
        (c[1] / c[0] / brate["exploration"], w, c)
        for w, c in counts["exploration"].items()
        if c[0] >= 40
    ]
    candidates.sort(reverse=True)
    top = []
    for lift, w, c in candidates[:25]:
        v = counts["validation"].get(w, [0, 0])
        top.append(
            {
                "word": w,
                "explore_n": c[0],
                "explore_rate": round(c[1] / c[0], 3),
                "explore_lift": round(lift, 2),
                "valid_n": v[0],
                "valid_rate": round(v[1] / v[0], 3) if v[0] else None,
                "valid_lift": round(v[1] / v[0] / brate["validation"], 2) if v[0] else None,
            }
        )
    print(json.dumps({
        "spikes": dict(sorted(summary.items())),
        "move_words_share": {"before": round(before_move_words / max(before_n, 1), 3), "after": round(after_move_words / max(after_n, 1), 3)},
        "article_base_rate": {p: {"articles": base[p][0], "followed_by_up_spike": base[p][1], "rate": round(brate[p], 4)} for p in base},
        "top_exploration_words_checked_on_validation": top,
        "examples": examples,
    }, indent=1, default=str))


asyncio.run(main())
