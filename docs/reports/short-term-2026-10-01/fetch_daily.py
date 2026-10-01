"""Fetch split/dividend-adjusted daily bars (SIP) for a fixed universe into a CSV.

Universe fixed before any strategy was run: S&P 100 constituents as recalled for
2025-26, plus SPY. This is a current-constituent list, so it carries survivorship
bias (today's large caps were mostly yesterday's winners). Read-only market data.
"""

import csv
import sys
import time

import httpx

sys.path.insert(0, "backend")
from stockbrain.config import Settings  # noqa: E402

UNIVERSE = """AAPL ABBV ABT ACN ADBE AIG AMD AMGN AMT AMZN AVGO AXP BA BAC BK BKNG BLK BMY BRK.B C
CAT CHTR CL CMCSA COF COP COST CRM CSCO CVS CVX DE DHR DIS DUK EMR F FDX GD GE GILD GM GOOG GOOGL
GS HD HON IBM INTC INTU ISRG JNJ JPM KO LIN LLY LMT LOW MA MCD MDLZ MDT MET META MMM MO MRK MS MSFT
NEE NFLX NKE NVDA ORCL PEP PFE PG PM PYPL QCOM RTX SBUX SCHW SO SPG T TGT TMO TMUS TSLA TXN UNH UNP
UPS USB V VZ WFC WMT XOM PLTR UBER ANET SPY""".split()

settings = Settings()
headers = {
    "APCA-API-KEY-ID": settings.alpaca_api_key.get_secret_value(),
    "APCA-API-SECRET-KEY": settings.alpaca_api_secret.get_secret_value(),
}
out = open(sys.argv[1], "w", newline="")
writer = csv.writer(out)
writer.writerow(["symbol", "date", "open", "high", "low", "close", "volume"])
rows = 0
with httpx.Client(base_url="https://data.alpaca.markets", headers=headers, timeout=60) as client:
    for i in range(0, len(UNIVERSE), 20):
        batch = UNIVERSE[i : i + 20]
        token = None
        while True:
            params = {
                "symbols": ",".join(batch),
                "timeframe": "1Day",
                "start": "2019-01-01T00:00:00Z",
                "end": "2026-09-30T00:00:00Z",
                "adjustment": "all",
                "feed": "sip",
                "limit": 10000,
            }
            if token:
                params["page_token"] = token
            response = client.get("/v2/stocks/bars", params=params)
            response.raise_for_status()
            body = response.json()
            for symbol, bars in (body.get("bars") or {}).items():
                for b in bars:
                    writer.writerow([symbol, b["t"][:10], b["o"], b["h"], b["l"], b["c"], b["v"]])
                    rows += 1
            token = body.get("next_page_token")
            time.sleep(0.4)
            if not token:
                break
        print("batch", i // 20, "rows so far", rows, flush=True)
out.close()
print("done", rows)
