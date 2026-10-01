"""For every >=2% relative gap, record overnight news for that symbol (resumable JSONL)."""

import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

sys.path.insert(0, "backend")
from stockbrain.config import Settings  # noqa: E402

NY = ZoneInfo("America/New_York")
data, out_path = sys.argv[1], sys.argv[2]
raw = pd.read_csv(data, parse_dates=["date"])
O = raw.pivot(index="date", columns="symbol", values="open").sort_index()
C = raw.pivot(index="date", columns="symbol", values="close").sort_index()
g = O / C.shift(1) - 1
rg = g.sub(g["SPY"], axis=0).drop(columns="SPY")
dates = list(C.index)
events = []
for i in range(1, len(dates)):
    row = rg.iloc[i]
    for sym, val in row.items():
        if pd.notna(val) and abs(val) >= 0.02:
            events.append((sym, dates[i - 1].date(), dates[i].date(), float(val)))

done = set()
if os.path.exists(out_path):
    for line in open(out_path):
        rec = json.loads(line)
        done.add((rec["symbol"], rec["date"]))

settings = Settings()
headers = {
    "APCA-API-KEY-ID": settings.alpaca_api_key.get_secret_value(),
    "APCA-API-SECRET-KEY": settings.alpaca_api_secret.get_secret_value(),
}
with open(out_path, "a") as out, httpx.Client(
    base_url="https://data.alpaca.markets", headers=headers, timeout=30
) as client:
    for k, (sym, prev, day, val) in enumerate(events):
        if (sym, str(day)) in done:
            continue
        start = dt.datetime.combine(prev, dt.time(16, 0), NY).astimezone(dt.UTC)
        end = dt.datetime.combine(day, dt.time(9, 30), NY).astimezone(dt.UTC)
        params = {
            "symbols": sym.replace(".", "/") if "." in sym else sym,
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "limit": 50,
            "sort": "asc",
        }
        for attempt in range(5):
            try:
                resp = client.get("/v1beta1/news", params=params)
                if resp.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                items = resp.json().get("news", [])
                break
            except httpx.HTTPError:
                time.sleep(3 * (attempt + 1))
        else:
            items = None  # recorded as unknown, never as "no news"
        focused = None if items is None else [
            {"t": it.get("created_at"), "h": (it.get("headline") or "")[:120], "n": len(it.get("symbols") or [])}
            for it in items
            if len(it.get("symbols") or []) <= 3
        ]
        out.write(json.dumps({
            "symbol": sym, "date": str(day), "gap": val,
            "all_items": None if items is None else len(items),
            "focused": focused,
        }) + "\n")
        out.flush()
        time.sleep(0.34)
        if k % 500 == 0:
            print(k, "/", len(events), flush=True)
print("done", len(events))
