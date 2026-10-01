"""Analyse gaps split by overnight news: event-level and 10-slot portfolio."""

import json
import statistics
import sys
from collections import Counter, defaultdict

S = sys.argv[1]
side = float(sys.argv[2]) if len(sys.argv) > 2 else 0.002
sys.argv = ["backtest.py", f"{S}/daily.csv", str(side)]
sys.path.insert(0, S)
import backtest as bt  # noqa: E402
import pandas as pd  # noqa: E402

news = {}
for line in open(f"{S}/gap_news.jsonl"):
    r = json.loads(line)
    news[(r["symbol"], r["date"])] = r
dates = bt.dates
buckets = defaultdict(list)
per_year = Counter()
unknown = 0
for i in range(1, bt.n):
    day = str(dates[i].date())
    for sym in bt.stocks:
        rec = news.get((sym, day))
        if rec is None:
            continue
        if rec["focused"] is None:
            unknown += 1
            continue
        has = bool(rec["focused"])
        per_year[(dates[i].year, has)] += 1
        kind = f"gap_{'down' if rec['gap'] < 0 else 'up'}_{'news' if has else 'no_news'}"
        x = i + 5
        if x >= bt.n:
            continue
        col = bt.C.columns.get_loc(sym)
        entry, exit_ = bt.O.iat[i, col], bt.C.iat[x, col]
        if pd.isna(entry) or pd.isna(exit_):
            continue
        spy = bt.C.columns.get_loc("SPY")
        raw = (exit_ * (1 - side)) / (entry / (1 - side)) - 1
        rel = raw - (bt.C.iat[x, spy] / bt.O.iat[i, spy] - 1)
        period = "exploration" if dates[i] <= pd.Timestamp("2023-12-31") else "validation"
        buckets[(kind, period)].append((i, sym, raw, rel, abs(rec["gap"])))


def summary(rows):
    if not rows:
        return {"n": 0}
    raw = [r[2] for r in rows]
    rel = [r[3] for r in rows]
    return {
        "n": len(rows),
        "days": len({r[0] for r in rows}),
        "mean_pct": round(statistics.fmean(raw) * 100, 2),
        "median_pct": round(statistics.median(raw) * 100, 2),
        "win": round(sum(x > 0 for x in raw) / len(raw), 3),
        "vs_spy_mean_pct": round(statistics.fmean(rel) * 100, 2),
        "vs_spy_median_pct": round(statistics.median(rel) * 100, 2),
    }


report = {"side_cost": side, "unknown_news": unknown, "events": {}, "portfolio": {}, "coverage": {}}
for (kind, period), rows in sorted(buckets.items()):
    report["events"][f"{kind}|{period}"] = summary(rows)
for year in sorted({y for y, _h in per_year}):
    total = per_year[(year, True)] + per_year[(year, False)]
    report["coverage"][year] = {"gaps": total, "share_with_news": round(per_year[(year, True)] / total, 3)}
# portfolio: pre-registered B and controls, same engine as the other strategies
for kind in ("gap_down_no_news", "gap_down_news", "gap_up_news", "gap_up_no_news"):
    trades = []
    for (k, _p), rows in buckets.items():
        if k != kind:
            continue
        for i, sym, _raw, _rel, size in rows:
            trades.append(bt.Trade(sym, i, i + 5, "close", size))
    for period, (lo, hi) in bt.PERIODS.items():
        curve, closed = bt.simulate(trades, lo, hi)
        m = bt.metrics(curve, closed)
        m.pop("_first")
        report["portfolio"][f"{kind}|{period}"] = m
print(json.dumps(report, indent=1, default=str))
