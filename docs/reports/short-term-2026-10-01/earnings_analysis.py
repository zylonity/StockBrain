"""Post-earnings drift: E1 double beat, E2 EPS surprise >= +5%, control E0 double miss."""
import datetime as dt, json, re, statistics, sys
from zoneinfo import ZoneInfo
S = sys.argv[1]; side = float(sys.argv[2]) if len(sys.argv) > 2 else 0.002
sys.argv = ["backtest.py", f"{S}/daily.csv", str(side)]; sys.path.insert(0, S)
import backtest as bt  # noqa: E402
import pandas as pd  # noqa: E402

NY = ZoneInfo("America/New_York")
NUM = r"(\$?\(?\$?-?\d+(?:\.\d+)?\)?)"
EPS = re.compile(r"EPS\s+" + NUM + r"\s+(Beats?|Miss(?:es)?|In-?\s?Line(?: With)?)\s+" + NUM + r"\s+Estimate", re.I)
EPS_INLINE = re.compile(r"EPS\s+" + NUM + r",\s*In-?\s?line\b", re.I)
SALES = re.compile(r"(?:Sales|Revenue)\s+\$?[\d.]+\s?[BMK]?\s+(Beats?|Miss(?:es)?|In-?\s?Line(?: With)?)", re.I)


def value(text):
    """'$(1.14)' and '($1.14)' are negative; '$1.14' is positive."""
    number = float(text.replace("$", "").replace("(", "").replace(")", ""))
    return -abs(number) if "(" in text else number


days = list(bt.dates)
open_times = [dt.datetime.combine(d.date(), dt.time(9, 30), NY) for d in days]

events, seen, unparsed = [], set(), 0
for line in open(f"{S}/earnings.jsonl"):
    r = json.loads(line)
    if len(r.get("symbols") or []) > 3:
        continue
    m = EPS.search(r["h"])
    if m:
        actual, verb, est = value(m.group(1)), m.group(2).lower(), value(m.group(3))
    else:
        inline = EPS_INLINE.search(r["h"])
        if not inline:
            unparsed += 1; continue
        actual = est = value(inline.group(1)); verb = "inline"
    t = dt.datetime.fromisoformat(r["t"].replace("Z", "+00:00"))
    key = (r["symbol"], t.astimezone(NY).date())
    if key in seen:
        continue
    seen.add(key)
    sales = SALES.search(r["h"])
    sales_verb = sales.group(1).lower() if sales else None
    i = next((k for k, ot in enumerate(open_times) if ot > t), None)
    if i is None or r["symbol"] not in bt.C.columns:
        continue
    surprise = (actual - est) / abs(est) if est else None
    events.append({"sym": r["symbol"], "i": i, "eps_beat": verb.startswith("beat"), "eps_miss": verb.startswith("miss"),
                   "sales_beat": bool(sales_verb and sales_verb.startswith("beat")),
                   "sales_miss": bool(sales_verb and sales_verb.startswith("miss")), "surprise": surprise})

rules = {
    "E1_double_beat": lambda e: e["eps_beat"] and e["sales_beat"],
    "E2_eps_surprise_5pct": lambda e: e["surprise"] is not None and e["surprise"] >= 0.05,
    "E0_double_miss_control": lambda e: e["eps_miss"] and e["sales_miss"],
    "all_earnings": lambda e: True,
}
HOLD = 20
report = {"side_cost": side, "parsed_events": len(events), "unparsed_headlines": unparsed, "events": {}, "portfolio": {}}
spy = bt.C.columns.get_loc("SPY")
for name, rule in rules.items():
    trades = []
    for period, (lo, hi) in bt.PERIODS.items():
        rows = []
        for e in events:
            if not rule(e):
                continue
            i, x = e["i"], e["i"] + HOLD
            if x >= bt.n or not (pd.Timestamp(lo) <= days[i] <= pd.Timestamp(hi)):
                continue
            col = bt.C.columns.get_loc(e["sym"])
            entry, exit_ = bt.O.iat[i, col], bt.C.iat[x, col]
            if pd.isna(entry) or pd.isna(exit_):
                continue
            raw = exit_ * (1 - side) / (entry / (1 - side)) - 1
            spy_net = bt.C.iat[x, spy] * (1 - side) / (bt.O.iat[i, spy] / (1 - side)) - 1
            rows.append((raw, raw - spy_net))
            trades.append(bt.Trade(e["sym"], i, x, "close", e["surprise"] or 0.0))
        if rows:
            raw = [r[0] for r in rows]; rel = [r[1] for r in rows]
            report["events"][f"{name}|{period}"] = {
                "n": len(rows), "mean_pct": round(100 * statistics.fmean(raw), 2), "median_pct": round(100 * statistics.median(raw), 2),
                "win": round(sum(v > 0 for v in raw) / len(raw), 3), "vs_spy_mean_pct": round(100 * statistics.fmean(rel), 2),
                "vs_spy_median_pct": round(100 * statistics.median(rel), 2), "beat_spy": round(sum(v > 0 for v in rel) / len(rel), 3)}
    if not name.startswith("E0") and name != "all_earnings":
        for period, (lo, hi) in bt.PERIODS.items():
            curve, closed = bt.simulate(trades, lo, hi)
            m = bt.metrics(curve, closed); m.pop("_first")
            report["portfolio"][f"{name}|{period}"] = m
print(json.dumps(report, indent=1))
