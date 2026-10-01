"""Grade every live research verdict against later prices (gross, vs SPY)."""
import csv, datetime as dt, re, statistics, sys
from collections import defaultdict
from zoneinfo import ZoneInfo
S = sys.argv[1]; NY = ZoneInfo("America/New_York")
px = defaultdict(dict)
for r in csv.DictReader(open(f"{S}/thesis_daily.csv")):
    px[r["symbol"]][r["date"]] = (float(r["open"]), float(r["close"]))
days = sorted(px["SPY"])
REACTION = re.compile(r"what'?s going on|why is .* (stock|shares)|stock (is )?(rising|falling|gaining|soaring|jumping|trading higher|trading lower)|shares (are )?(rising|falling|trading)|hits? (fresh |new )?(record|all-time)|stock market today|movers|(up|down) \d+%|rises as|falls as|soars|surges|jumps|plunges|tumbles", re.I)
STALE = re.compile(r"transcript|earnings call|conference call|recap|weekly|vs\.|versus|\d+ stocks? (to|analysts)|top \d+|here'?s", re.I)
def kind(h):
    if STALE.search(h or ""): return "stale_or_opinion"
    if REACTION.search(h or ""): return "reaction_to_move"
    return "other"
res = defaultdict(lambda: defaultdict(list))
seen = set()
for r in csv.DictReader(open(f"{S}/theses.csv")):
    sym = r["market_symbol"]
    if r["currency"] != "USD" or sym not in px or r["trigger"] != "EVENT": continue
    t = dt.datetime.fromisoformat(r["completed_at"]).astimezone(NY)
    # entry: next regular-session open strictly after the verdict
    entry_day = next((d for d in days if dt.datetime.combine(dt.date.fromisoformat(d), dt.time(9, 30), NY) > t), None)
    if entry_day is None or entry_day not in px[sym]: continue
    i = days.index(entry_day)
    action = r["action"]; group = "BUY" if action == "BUY" else ("SELL/REDUCE" if action in ("SELL","REDUCE") else "HOLD/NO_ACTION")
    dup = (sym, action, r["headline"]) in seen; seen.add((sym, action, r["headline"]))
    if dup: continue  # one observation per symbol/verdict/headline
    for label, k in (("d0_close", 0), ("d1_close", 1), ("d3_close", 3)):
        if i + k >= len(days) or days[i + k] not in px[sym]: continue
        so, _ = px[sym][entry_day]; sc = px[sym][days[i + k]][1]
        mo, _ = px["SPY"][entry_day]; mc = px["SPY"][days[i + k]][1]
        rel = (sc / so - 1) - (mc / mo - 1)
        for key in (group, f"{group}|{kind(r['headline'])}", f"{group}|executed" if r["executed"] != "0" else f"{group}|not_executed", f"{group}|conf>=0.75" if float(r["confidence"]) >= 0.75 else f"{group}|conf<0.75"):
            res[key][label].append(rel)
def fmt(v):
    if not v: return "n=0"
    return "n=%4d mean %+5.2f%% med %+5.2f%% beat SPY %3.0f%%" % (len(v), 100*statistics.fmean(v), 100*statistics.median(v), 100*sum(x > 0 for x in v)/len(v))
for key in sorted(res):
    print(key)
    for label in ("d0_close", "d1_close", "d3_close"):
        print("   %-9s %s" % (label, fmt(res[key][label])))
